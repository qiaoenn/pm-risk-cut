"""Refusal logic.  Nothing in this project connects or trades without passing here.

The guards are deliberately paranoid and deliberately dumb: each one checks a
single fact and raises.  They exist because the failure mode this project is
one typo away from is "flatten a real portfolio", and that is not a failure you
get to undo.
"""

import tomllib
from pathlib import Path

PAPER_PORTS = {7497: "Trader Workstation, paper",
               4002: "IB Gateway, paper"}
LIVE_PORTS = {7496: "Trader Workstation, LIVE",
              4001: "IB Gateway, LIVE"}

# IBKR paper account codes begin with D (DU individual, DF advisor/FA master,
# DI institution).  This is a heuristic, not a guarantee, so it warns rather
# than refuses -- the port check is the one that actually has teeth.
PAPER_PREFIXES = ("DU", "DF", "DI")


class GuardFailure(RuntimeError):
    """Raised when a precondition for touching an account is not met."""


def load_config(path: Path) -> dict:
    if not path.exists():
        raise GuardFailure(
            f"{path.name} not found.\n"
            f"  cp config.example.toml {path.name}\n"
            f"then fill in the port and the target sub-accounts."
        )
    return tomllib.loads(path.read_text())


def assert_paper_port(port: int) -> None:
    """Hard refusal on any port that is not a known paper port.

    Unknown ports are refused too.  A port we do not recognise is not
    evidence of safety, and someone forwarding 4001 to 7497 is exactly the
    kind of clever setup this needs to survive.
    """
    if port in LIVE_PORTS:
        raise GuardFailure(
            f"REFUSING: port {port} is {LIVE_PORTS[port]}. "
            f"This project only ever runs against paper."
        )
    if port not in PAPER_PORTS:
        raise GuardFailure(
            f"REFUSING: port {port} is not a recognised paper port. "
            f"Expected one of {sorted(PAPER_PORTS)}."
        )


def assert_targets_declared(targets) -> list:
    """There is no 'every managed account' mode, by design."""
    targets = [t.strip() for t in (targets or []) if t.strip()]
    if not targets:
        raise GuardFailure(
            "REFUSING: [accounts].targets is empty. Every account this may "
            "touch must be listed explicitly -- there is no wildcard."
        )
    return targets


def assert_targets_managed(targets, managed) -> list[str]:
    """Every target must actually be visible from this login.

    Returns a list of human-readable warnings (currently: account codes that
    do not look like paper accounts) for the caller to surface and log.
    """
    missing = [t for t in targets if t not in managed]
    if missing:
        raise GuardFailure(
            f"REFUSING: {', '.join(missing)} not visible from this login.\n"
            f"  Visible accounts: {', '.join(managed) or '(none)'}\n"
            f"Either the code is wrong or you are logged into the wrong master."
        )
    return [f"{t} does not look like a paper account code "
            f"(expected a {'/'.join(PAPER_PREFIXES)} prefix) -- verify before arming."
            for t in targets if not t.startswith(PAPER_PREFIXES)]


def assert_armed(armed: bool, what: str) -> None:
    """Mutating actions require --arm on top of naming the thing explicitly."""
    if not armed:
        raise GuardFailure(
            f"REFUSING: {what} would change account state. Re-run with --arm "
            f"once you have read the dry-run output above."
        )
