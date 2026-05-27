"""
virtual_plant.py
================
Kombiblock Virtual Plant — Biological SBR Reactor Simulator
Async OPC UA communication to TIA Portal / PLCSIM Advanced V4.0

SETUP
-----
  PLC_URL  = opc.tcp://192.168.0.1:48400
  OPC_USER = ""  (anonymous — already configured in TIA Portal)
  NODE_IDS = as seen in UAExpert (plant_io DB, German variable names)
"""

import asyncio
import logging
import queue
from dataclasses import dataclass
import json
from pathlib import Path

from asyncua import Client, Node, ua

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("VirtualPlant")

CONFIG_PATH = Path(__file__).with_name("config.json")

with CONFIG_PATH.open("r", encoding="utf-8") as f:
    config = json.load(f)

PLC_URL = config["PLC_URL"]
OPC_USER = config.get("OPC_USER", "")
OPC_PASS = config.get("OPC_PASS", "")
NODE_IDS = config["NODE_IDS"]

# Friendly names used throughout the code — must match NODE_IDS keys exactly
ACTUATOR_KEYS = ("zulauf_ventil", "ablass_ventil", "kompressor")
SENSOR_KEYS   = ("abwasser_fuellstand", "ammonium", "nitrat", "sauerstoff_konzentration")

# ─────────────────────────────────────────────────────────────────────────────
# ③ SIMULATION PARAMETERS  (from Kombiblock.xml)
# ─────────────────────────────────────────────────────────────────────────────
DT               = 1.0      # time-step [s] — matches PLC exec_time T#1S
SIM_SPEED        = 3000.0    # Run 500x faster than real time
PUBLISH_INTERVAL = 100      # OPC UA subscription interval [ms]
WRITE_INTERVAL   = 0.1      # sensor write-back interval [s]
RECONNECT_DELAY  = 5        # seconds between reconnect attempts
TICK_INTERVAL    = DT / SIM_SPEED

# ── GUI BRIDGE QUEUES ────────────────────────────────────────
actuator_queue: queue.Queue = queue.Queue()           # GUI → asyncio
sensor_queue:   queue.Queue = queue.Queue(maxsize=1)  # asyncio → GUI

Q_ZU             = 666.66
Q_AB             = 500.0
NH4_INFLOW_CONC  = 10.0
ABBAURATE_NH4    = 10.0     # [mg/l/min]
ABBAURATE_NO3    = 8.0      # [mg/l/min]
VOLUME_MAX       = 40000.0
NH4_ABS_MAX      = 400000.0
NO3_ABS_MAX      = 400000.0


# ─────────────────────────────────────────────────────────────────────────────
# ④ SHARED STATE  (single asyncio thread — no locks needed)
# ─────────────────────────────────────────────────────────────────────────────
class ActuatorState:
    """Current actuator commands received from TIA Portal."""
    def __init__(self):
        self.zulauf_ventil: bool = False    # inlet valve
        self.ablass_ventil: bool = False    # outlet valve
        self.kompressor:    bool = False    # compressor / aeration

    def update(self, key: str, value: bool):
        setattr(self, key, bool(value))


@dataclass
class SensorReadings:
    """Computed sensor values — written back to TIA Portal."""
    abwasser_fuellstand:     float = 0.0   # tank level  [%]
    ammonium:                float = 0.0   # NH4         [mg/l]
    nitrat:                  float = 0.0   # NO3         [mg/l]
    sauerstoff_konzentration:float = 0.0   # O2          [mg/l]
    volume:                  float = 0.0   # internal    [l]

    # ── Actuator feedback ────────────────────────────────────
    zulauf_ventil: bool = False
    ablass_ventil: bool = False
    kompressor: bool = False

@dataclass
class ReactorState:
    """Persistent internal reactor state between ticks."""
    volume:       float = 0.0
    nh4_absolute: float = 0.0
    no3_absolute: float = 0.0
    tick:         int   = 0


# ─────────────────────────────────────────────────────────────────────────────
# ⑤ PHYSICS ENGINE  (conc function block — Kombiblock.xml)
# ─────────────────────────────────────────────────────────────────────────────
def _clamp(v, lo, hi): return max(lo, min(hi, v))

def run_conc(reactor: ReactorState, v1: bool, v2: bool, airflow: bool) -> SensorReadings:
    """
    One simulation tick. Steps mirror the PLC Structured Text exactly:
      1.  Volume       ← inflow / outflow
      2.  NH4 mass     ← inflow load
      3.  NH4 conc     (intermediate)
      4.  NH4 mass     ← outflow loss
      5.  NH4 mass     ← nitrification (airflow ON)
      6.  NO3 mass     ← build-up 1:1 from NH4 (airflow ON)
      7.  NO3 conc     (intermediate)
      8.  NO3 mass     ← denitrification (airflow OFF)
      9.  NO3 mass     ← outflow loss
      10. Clamp absolutes
      11. Final concentrations
      12. O2 = 2.0 if airflow else 0.0
      13. Level = volume / 400
    """
    reactor.tick += 1
    dt = DT

    # 1. Volume
    if v1: reactor.volume += Q_ZU * dt * 0.001
    if v2: reactor.volume -= Q_AB * dt * 0.001
    reactor.volume = _clamp(reactor.volume, 0.0, VOLUME_MAX)
    sv = max(reactor.volume, 1.0)

    # 2. NH4 inflow mass
    if v1: reactor.nh4_absolute += Q_ZU * NH4_INFLOW_CONC * dt * 0.001

    # 3–4. NH4 intermediate conc → outflow
    nh4_c = reactor.nh4_absolute / sv
    if v2: reactor.nh4_absolute -= Q_AB * nh4_c * dt * 0.001

    # 5. Nitrification
    if airflow:
        reactor.nh4_absolute -= reactor.volume * (ABBAURATE_NH4 / 60.0) * dt * 0.001

    # 6. NO3 build-up
    amp = 1 if reactor.nh4_absolute >= 1.0 else 0
    if airflow:
        reactor.no3_absolute += reactor.volume * (ABBAURATE_NH4 / 60.0) * amp * dt * 0.001

    # 7–8. NO3 intermediate conc → denitrification
    no3_c = reactor.no3_absolute / sv
    if not airflow:
        reactor.no3_absolute -= reactor.volume * (ABBAURATE_NO3 / 60.0) * dt * 0.001

    # 9. NO3 outflow
    if v2: reactor.no3_absolute -= Q_AB * no3_c * dt * 0.001

    # 10. Clamp
    reactor.nh4_absolute = _clamp(reactor.nh4_absolute, 0.0, NH4_ABS_MAX)
    reactor.no3_absolute = _clamp(reactor.no3_absolute, 0.0, NO3_ABS_MAX)

    # 11–13. Final outputs
    sv = max(reactor.volume, 1.0)
    return SensorReadings(
        abwasser_fuellstand      = reactor.volume / 400.0,
        ammonium                 = reactor.nh4_absolute / sv,
        nitrat                   = reactor.no3_absolute / sv,
        sauerstoff_konzentration = 2.0 if airflow else 0.0,
        volume                   = reactor.volume,
    )


# ─────────────────────────────────────────────────────────────────────────────
# ⑥ SUBSCRIPTION HANDLER  (no import / no base class needed in asyncua)
# ─────────────────────────────────────────────────────────────────────────────
class ActuatorHandler:
    """
    asyncua calls datachange_notification() via duck-typing whenever
    a subscribed actuator node changes on the PLC.
    """
    def __init__(self, actuators: ActuatorState, id_to_key: dict):
        self._actuators  = actuators
        self._id_to_key  = id_to_key   # node identifier (int/str) → ActuatorState key

    def datachange_notification(self, node: Node, val, data):
        key = self._id_to_key.get(node.nodeid.Identifier)
        if key is not None:
            self._actuators.update(key, val)
            log.info(f"  ← PLC [{key}] = {val}")

    def event_notification(self, event):
        pass


# ─────────────────────────────────────────────────────────────────────────────
# ⑦ SIMULATION TASK
# ─────────────────────────────────────────────────────────────────────────────
async def simulation_task(actuators: ActuatorState, sensors: SensorReadings, reactor: ReactorState):
    log.info("Simulation task started (runs independently of OPC UA).")
    while True:
        t0  = asyncio.get_event_loop().time()

        # ── ① DRAIN GUI actuator commands
        try:
            while True:
                cmd = actuator_queue.get_nowait()
                for k, v in cmd.items():
                    actuators.update(k, v)
        except queue.Empty:
            pass

        out = run_conc(
            reactor,
            v1      = actuators.zulauf_ventil,
            v2      = actuators.ablass_ventil,
            airflow = actuators.kompressor,
        )

        # ── attach actuator states so GUI can reflect them ──
        out.zulauf_ventil = actuators.zulauf_ventil
        out.ablass_ventil = actuators.ablass_ventil
        out.kompressor    = actuators.kompressor

        # Update shared sensor readings
        sensors.abwasser_fuellstand      = out.abwasser_fuellstand
        sensors.ammonium                 = out.ammonium
        sensors.nitrat                   = out.nitrat
        sensors.sauerstoff_konzentration = out.sauerstoff_konzentration
        sensors.volume                   = out.volume

        # ── ② PUSH latest readings to GUI
        try:
            sensor_queue.get_nowait()    # discard stale value if GUI hasn't read it yet
        except queue.Empty:
            pass
        try:
            sensor_queue.put_nowait(out)
        except queue.Full:
            pass

        if reactor.tick % (10 * int(SIM_SPEED)) == 0:   # ← log every 10 simulated minutes
            log.info(
                f"  t={reactor.tick:>6}s | "
                f"Level={sensors.abwasser_fuellstand:>5.1f}%  "
                f"NH4={sensors.ammonium:>7.4f} mg/l  "
                f"NO3={sensors.nitrat:>7.4f} mg/l  "
                f"O2={sensors.sauerstoff_konzentration:.1f} mg/l  "
                f"Vol={sensors.volume:>7.0f} L  "
                f"[zulauf={actuators.zulauf_ventil}  "
                f"ablass={actuators.ablass_ventil}  "
                f"komp={actuators.kompressor}]"
            )

        # sleep for real tick interval, not DT    
        await asyncio.sleep(max(0.0, TICK_INTERVAL - (asyncio.get_event_loop().time() - t0)))


# ─────────────────────────────────────────────────────────────────────────────
# ⑧ WRITER TASK
# ─────────────────────────────────────────────────────────────────────────────
async def writer_task(nodes: dict, sensors: SensorReadings):
    log.info("Writer task started.")
    while True:
        write_map = {
            "abwasser_fuellstand"     : float(sensors.abwasser_fuellstand),
            "ammonium"                : float(sensors.ammonium),
            "nitrat"                  : float(sensors.nitrat),
            "sauerstoff_konzentration": float(sensors.sauerstoff_konzentration),
        }
        for key, value in write_map.items():
            try:
                await nodes[key].write_value(
                    ua.DataValue(ua.Variant(value, ua.VariantType.Float)
                    )
                )
            except Exception as e:
                log.warning(f"  Write failed [{key}]: {e}")

        log.debug(
            f"  → PLC | Level={write_map['abwasser_fuellstand']:.1f}%  "
            f"NH4={write_map['ammonium']:.4f}  "
            f"NO3={write_map['nitrat']:.4f}  "
            f"O2={write_map['sauerstoff_konzentration']:.1f}"
        )
        await asyncio.sleep(WRITE_INTERVAL)


# ─────────────────────────────────────────────────────────────────────────────
# ⑨ MAIN — reconnect loop
# ─────────────────────────────────────────────────────────────────────────────
async def main():
    actuators = ActuatorState()
    sensors   = SensorReadings()
    reactor   = ReactorState()

    sim = asyncio.create_task(simulation_task(actuators, sensors, reactor))

    log.info(f"Connecting to: {PLC_URL}")

    while True:
        try:
            client = Client(url=PLC_URL)

            # Credentials — skipped when empty (anonymous)
            if OPC_USER:
                client.set_user(OPC_USER)
                client.set_password(OPC_PASS)

            async with client:
                log.info("✓ Connected to TIA Portal OPC UA server.")

                # Resolve all nodes
                nodes = {key: client.get_node(nid) for key, nid in NODE_IDS.items()}

                # Build reverse map: node identifier → ActuatorState attribute name
                id_to_key = {
                    nodes[key].nodeid.Identifier: key
                    for key in ACTUATOR_KEYS
                }

                # Subscribe to actuator nodes (PLC pushes changes to Python)
                handler      = ActuatorHandler(actuators, id_to_key)
                subscription = await client.create_subscription(PUBLISH_INTERVAL, handler)
                await subscription.subscribe_data_change([nodes[k] for k in ACTUATOR_KEYS])
                log.info(f"✓ Subscribed to actuator nodes ({PUBLISH_INTERVAL} ms interval).")

                # Read initial actuator values immediately on connect
                for key in ACTUATOR_KEYS:
                    val = await nodes[key].read_value()
                    actuators.update(key, val)
                    log.info(f"  Initial [{key}] = {val}")

                # Start writer task
                writer = asyncio.create_task(writer_task(nodes, sensors))
                log.info("Virtual plant running. Press Ctrl+C to stop.\n")

                try:
                    await asyncio.gather(writer)
                except asyncio.CancelledError:
                    pass
                finally:
                    writer.cancel()

        except asyncio.CancelledError:
            log.info("Shutdown — stopping virtual plant.")
            sim.cancel()
            break

        except Exception as e:
            log.error(f"OPC UA error: {e}")
            log.info(f"Reconnecting in {RECONNECT_DELAY}s ...")
            await asyncio.sleep(RECONNECT_DELAY)


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Virtual plant stopped.")
