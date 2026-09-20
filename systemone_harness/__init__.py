"""A harness for System One models.

A System One model is a decision function: state in, typed decision with a probability out, in a
fixed time. It does not plan, generate, act or remember. This package supplies what it does not:
the action space it may choose from, the state it decides on, the environment that executes its
choice, and the loop that runs until a terminal state.
"""
from .actions import Action, ActionSpace, ActionSpaceError, Guard, Param
from .controller import Controller
from .encoder import StateEncoder
from .environment import Environment, Observation, Result
from .gate import Gate, Verdict
from .provider import (DecisionProvider, OpenRouterProvider, ProviderError, RecordedProvider,
                       TypeSafeProvider, provider_from_env)
from .trace import Run, Step, reasoning_text

__version__ = "0.2.0"
__all__ = ["Action", "ActionSpace", "ActionSpaceError", "Guard", "Param", "Controller", "StateEncoder",
           "Environment", "Observation", "Result", "Gate", "Verdict", "DecisionProvider",
           "OpenRouterProvider", "TypeSafeProvider", "RecordedProvider", "ProviderError",
           "provider_from_env", "Run", "Step", "reasoning_text"]
