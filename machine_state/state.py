"""Nelavi machine (rover operating) state evaluation.

A small, pure, deterministic evaluator. It uses only the evidence passed in:
no wall clock, no randomness, no network, no database, no hardware.

The eight state names are the owner-specified vocabulary for this milestone;
the frozen sources define no machine-state vocabulary. What each state means
here is a simulator-level implementation decision (see ARCHITECTURE_DECISIONS.md,
MS-1 to MS-3), not an industrial-machine claim and not a frozen requirement.

A sensor fault is NOT a machine fault (MD-4), so sensor status is never used as
evidence. Telemetry that does not exist (load, current, vibration, thermal) is
never invented: the "overloaded" state is never produced, and "fault" and
"maintenance" are produced only from explicit inputs.
"""

from dataclasses import dataclass

UNKNOWN = "unknown"
OFF = "off"
STARTING = "starting"
RUNNING = "running"
IDLE = "idle"
OVERLOADED = "overloaded"
FAULT = "fault"
MAINTENANCE = "maintenance"

STATES = (UNKNOWN, OFF, STARTING, RUNNING, IDLE, OVERLOADED, FAULT, MAINTENANCE)

# States that cannot be inferred from the available telemetry, and why.
NOT_INFERABLE_FROM_TELEMETRY = {
    OVERLOADED: "no load, current or similar evidence exists; this state is never produced",
    FAULT: "no machine-level fault signal exists in the telemetry; it is produced "
    "only from an explicit fault_reasons input",
    MAINTENANCE: "no maintenance signal exists in the telemetry; it is produced "
    "only when explicitly declared",
}


def _check_tristate(name, value):
    if value is not None and not isinstance(value, bool):
        raise ValueError("%s must be True, False or None" % name)


@dataclass(frozen=True)
class MachineEvidence:
    """The evidence the evaluator uses. None means "not reported"."""

    started: object = None  # the rover reports whether it is started
    homed: object = None  # startup homing is complete
    moving: object = None  # the rover is currently moving or turning
    fault_reasons: tuple = ()  # explicit machine-level fault signals
    maintenance_declared: bool = False  # maintenance mode explicitly declared

    def __post_init__(self):
        for name in ("started", "homed", "moving"):
            _check_tristate(name, getattr(self, name))
        if not isinstance(self.maintenance_declared, bool):
            raise ValueError("maintenance_declared must be True or False")
        reasons = self.fault_reasons
        if isinstance(reasons, (str, bytes)) or not isinstance(reasons, (tuple, list)):
            raise ValueError("fault_reasons must be a tuple or list of strings")
        if any(not isinstance(item, str) or item == "" for item in reasons):
            raise ValueError("each fault reason must be a non-empty string")
        object.__setattr__(self, "fault_reasons", tuple(reasons))


@dataclass(frozen=True)
class StateResult:
    state: str
    reason: str
    evidence: MachineEvidence
    previous_state: object = None  # the state passed in as previous, if any
    changed: bool = False  # True only when a previous state was given and differs


def _classify(e):
    # Precedence, highest first: fault, maintenance, unknown (not reported),
    # off, starting, running, idle, unknown (insufficient).
    if e.fault_reasons:
        return FAULT, "explicit fault signal reported: " + ", ".join(e.fault_reasons)
    if e.maintenance_declared:
        return MAINTENANCE, "maintenance mode is explicitly declared"
    if e.started is None:
        return UNKNOWN, "no evidence: it is not reported whether the rover is started"
    if e.started is False:
        if e.moving is True:
            return (
                UNKNOWN,
                "contradictory evidence: movement is reported while the rover is not started",
            )
        return OFF, "rover reports it is not started"
    if e.homed is False:
        return STARTING, "rover is started but its startup homing is not complete"
    if e.moving is True:
        return RUNNING, "rover is currently moving or turning"
    if e.moving is False and e.homed is True:
        return IDLE, "rover is started, startup is complete and the rover is stationary"
    if e.moving is None:
        return UNKNOWN, "insufficient evidence: movement is not reported"
    return UNKNOWN, "insufficient evidence: startup completion is not reported"


def evaluate(evidence, previous_state=None):
    """Return a StateResult for the evidence.

    previous_state, if given, must be one of STATES. It is only echoed back
    with a `changed` flag; no transition is ever refused or constrained.
    """
    if not isinstance(evidence, MachineEvidence):
        raise TypeError("evidence must be a MachineEvidence")
    if previous_state is not None and (
        not isinstance(previous_state, str) or previous_state not in STATES
    ):
        raise ValueError("previous_state must be one of the defined states or None")
    state, reason = _classify(evidence)
    changed = previous_state is not None and previous_state != state
    return StateResult(state, reason, evidence, previous_state, changed)


def evidence_from_telemetry(telemetry):
    """Build evidence from a Mock Rover telemetry snapshot.

    Uses only `started`, `camera.homed` and `running`. It deliberately ignores
    sensor status (a sensor fault is not a machine fault), the obstacle,
    battery, probe and link sections, and never supplies a fault or maintenance
    signal, because the telemetry has none. The snapshot is not modified.
    """
    if not isinstance(telemetry, dict):
        raise TypeError("telemetry must be a dict")
    camera = telemetry.get("camera")
    if camera is not None and not isinstance(camera, dict):
        raise ValueError("telemetry camera section must be an object")
    return MachineEvidence(
        started=telemetry.get("started"),
        homed=None if camera is None else camera.get("homed"),
        moving=telemetry.get("running"),
    )


def evaluate_telemetry(telemetry, previous_state=None):
    """Evaluate the state of a Mock Rover telemetry snapshot."""
    return evaluate(evidence_from_telemetry(telemetry), previous_state)
