# ---------------------------------------------------------------------------
# caliper shim: stdlib-only stand-ins for caliper_sdk.{grader, Rollout, GraderResult}
#
# This module is copied VERBATIM into every emitted artifact (reward_funcs.py,
# lambda_function.py, the OpenAI python grader) so they run where caliper is
# not installed. Rules for editing it:
#   * stdlib only, Python >= 3.10 compatible (no PEP 695, no 3.12-only syntax);
#   * no `from __future__` import (it would land mid-file once concatenated);
#   * semantics must mirror caliper_sdk._decorator (score in [0, 1], bools
#     rejected, dict -> Rollout coercion) - parity is enforced by tests.
# ---------------------------------------------------------------------------
import builtins as _builtins
import types as _types

_ROLLOUT_FIELDS = ("prompt", "response", "gold", "trace", "metadata")


class Rollout:
    """Minimal stand-in for caliper_sdk.Rollout (attribute access only)."""

    __slots__ = _ROLLOUT_FIELDS

    def __init__(self, prompt, response, gold=None, trace=None, metadata=None):
        if not isinstance(prompt, str) or not isinstance(response, str):
            raise TypeError("Rollout.prompt and Rollout.response must be str")
        if gold is not None and not isinstance(gold, str):
            raise TypeError("Rollout.gold must be str or None")
        self.prompt = prompt
        self.response = response
        self.gold = gold
        self.trace = list(trace) if trace is not None else []
        self.metadata = dict(metadata) if metadata is not None else {}

    @classmethod
    def model_validate(cls, data):
        unknown = set(data) - set(_ROLLOUT_FIELDS)
        if unknown:
            raise ValueError("unknown Rollout fields: " + ", ".join(sorted(unknown)))
        return cls(**data)

    def model_dump(self):
        return {f: getattr(self, f) for f in _ROLLOUT_FIELDS}

    def __repr__(self):
        fields = ", ".join(f + "=" + repr(getattr(self, f)) for f in _ROLLOUT_FIELDS)
        return "Rollout(" + fields + ")"


class GraderResult:
    """Minimal stand-in for caliper_sdk.GraderResult."""

    __slots__ = ("components", "explanation", "metadata", "score")

    def __init__(self, score, explanation=None, components=None, metadata=None):
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise TypeError("GraderResult.score must be a number")
        score = float(score)
        if not (0.0 <= score <= 1.0):
            raise ValueError("GraderResult.score " + repr(score) + " is outside [0.0, 1.0]")
        self.score = score
        self.explanation = explanation
        self.components = {k: float(v) for k, v in (components or {}).items()}
        self.metadata = dict(metadata) if metadata is not None else {}

    def __repr__(self):
        return "GraderResult(score=" + repr(self.score) + ")"


def _coerce_rollout(rollout):
    if isinstance(rollout, Rollout):
        return rollout
    if isinstance(rollout, dict):
        return Rollout.model_validate(rollout)
    raise TypeError("grader expected Rollout or dict, got " + type(rollout).__name__)


def _coerce_result(value):
    if isinstance(value, GraderResult):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return GraderResult(score=float(value))
    raise TypeError("grader must return float or GraderResult, got " + type(value).__name__)


def grader(func=None, /, *, name=None, version=None):
    """Stand-in for caliper_sdk.grader: bare `@grader` or `@grader(name=..., version=...)`."""

    def wrap(f):
        def wrapper(rollout):
            return _coerce_result(f(_coerce_rollout(rollout)))

        wrapper.__name__ = getattr(f, "__name__", "grader")
        wrapper.__qualname__ = getattr(f, "__qualname__", wrapper.__name__)
        wrapper.__doc__ = getattr(f, "__doc__", None)
        wrapper.__wrapped__ = f
        wrapper.name = name or wrapper.__name__
        wrapper.version = version or "0.1.0"
        return wrapper

    if func is not None:
        if not callable(func):
            raise TypeError("@grader must be applied to a callable")
        return wrap(func)
    return wrap


def _make_sdk_module():
    mod = _types.ModuleType("caliper_sdk")
    mod.grader = grader
    mod.Rollout = Rollout
    mod.GraderResult = GraderResult
    mod.types = mod  # `from caliper_sdk.types import Rollout` resolves here too
    return mod


_SDK_SHIM = _make_sdk_module()


def _looks_like_grader(obj):
    return (
        callable(obj)
        and hasattr(obj, "name")
        and hasattr(obj, "version")
        and hasattr(obj, "__wrapped__")
    )


def exec_grader_source(source, filename="<grader>"):
    """Exec grader `source` against the shim; return its namespace.

    `import caliper_sdk` / `from caliper_sdk[.types] import ...` inside the
    source resolve to the shim; every other import goes to the real importer
    (also for imports executed lazily inside grader bodies).
    """
    real_import = _builtins.__import__

    def _import(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and (name == "caliper_sdk" or name.startswith("caliper_sdk.")):
            return _SDK_SHIM
        return real_import(name, globals, locals, fromlist, level)

    sandbox_builtins = dict(vars(_builtins))
    sandbox_builtins["__import__"] = _import
    namespace = {"__name__": "caliper_grader", "__builtins__": sandbox_builtins}
    exec(compile(source, filename, "exec"), namespace)  # noqa: S102 - user's own grader file
    return namespace


def grader_names(namespace):
    """Sorted public names in `namespace` bound to @grader-decorated callables."""
    return sorted(
        k for k, v in namespace.items() if not k.startswith("_") and _looks_like_grader(v)
    )


def load_grader(source, attr=None, filename="<grader>"):
    """Exec `source` and return the grader bound to `attr` (or the only one)."""
    namespace = exec_grader_source(source, filename)
    if attr is None:
        found = grader_names(namespace)
        if len(found) != 1:
            raise LookupError(
                filename + " defines " + str(len(found)) + " graders (" + ", ".join(found)
                + "); select one with PATH.py:ATTR"
            )
        attr = found[0]
    obj = namespace.get(attr)
    if obj is None:
        raise LookupError(repr(attr) + " is not defined in " + filename)
    if not _looks_like_grader(obj):
        raise TypeError(repr(attr) + " in " + filename + " is not a @grader-decorated callable")
    return obj
