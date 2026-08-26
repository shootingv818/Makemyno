"""
The worker's pydantic request models must build without crashing.

THE BUG
-------
The models were defined INSIDE build_app(). pydantic v2 resolves a model's
annotations against the MODULE where the class lives, and a class defined inside a
function is in no module namespace — so a subclass annotation is an unresolvable
forward reference. Every model inherits from Account, so every one failed:

    pydantic.errors.PydanticUndefinedAnnotation: name 'StartLogin' is not defined

The worker container started, uvicorn never bound the port, and the master saw
only "Server disconnected without sending a response" — a symptom three steps
removed from the cause.

WHY IT SURVIVED THE TEST SUITE
------------------------------
The tests never built the real app: there was no pydantic stub, so worker_api
imported with models disabled. The suite exercised the endpoints through other
paths and the broken model construction was never triggered. tests/stubs.py now
ships a pydantic stand-in that reproduces the exact forward-reference resolution,
so this class of bug fails a test instead of a customer's server.
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def test_the_request_models_exist_at_module_level():
    """Importing the module builds the models. With the stub reproducing
    pydantic's forward-reference resolution, a model defined inside build_app()
    would raise here on import — exactly as it did on the real server."""
    import worker_api
    assert worker_api._HAVE_MODELS is True
    for name in ("Account", "StartLogin", "LoginCode", "SendStart", "Probe",
                 "GroupsSend", "SecretaryPass", "PvExport"):
        assert hasattr(worker_api, name), f"{name} must be a module-level model"


def test_every_model_subclasses_account():
    import worker_api
    for name in ("StartLogin", "LoginCode", "Prepare", "SendStart", "Probe"):
        model = getattr(worker_api, name)
        assert issubclass(model, worker_api.Account)


def test_the_models_are_not_defined_inside_build_app():
    """Structural guard: this is where they were, and where the forward reference
    could not resolve. Keep them out of the function."""
    tree = ast.parse(_src("worker_api.py"), filename="worker_api.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "build_app":
            inner = [n.name for n in ast.walk(node)
                     if isinstance(n, ast.ClassDef)]
            assert "Account" not in inner, (
                "request models must be module-level, not inside build_app "
                "(pydantic cannot resolve their forward references there)")


def test_the_stub_reproduces_the_forward_reference_crash():
    """Guards the guard. If the stub stopped enforcing this, the test above would
    pass against genuinely broken code and we would be blind again."""
    from pydantic import BaseModel, PydanticUndefinedAnnotation

    def build_inside_a_function():
        class Account(BaseModel):
            customer_id: int

        class StartLogin(Account):
            pass_key: "TotallyUndefinedName" = None
        return StartLogin

    with pytest.raises(PydanticUndefinedAnnotation):
        build_inside_a_function()


def test_a_plain_module_level_model_builds_fine():
    """The stub must not be so strict that it rejects valid models — otherwise it
    would just move the false alarm, not remove it."""
    from pydantic import BaseModel

    class Ok(BaseModel):
        a: int
        b: str = "x"

    instance = Ok(a=1)
    assert instance.a == 1


def test_a_model_can_be_instantiated_from_a_payload():
    """The endpoints receive these as parsed request bodies."""
    import worker_api
    body = worker_api.SendStart(customer_id=1001, phone="0912", targets=["g1"])
    assert body.customer_id == 1001
    assert body.targets == ["g1"]
