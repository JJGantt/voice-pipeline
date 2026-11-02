"""The wake-word model (openWakeWord, both wake words)."""

from openwakeword.model import Model

from . import config


def new_wake_model() -> Model:
    """One wake-word model PER CONNECTION. openWakeWord's Model is stateful (its melspectrogram and
    embedding buffers and the per-model score history), so a module-level one shared between two live
    sockets, an ESP reconnecting while its half-open predecessor is still being read, interleaves both
    streams' audio into one state and can fire, or miss, on the mix. ~80ms to build."""
    return Model(wakeword_models=[config.WAKE_MODEL, config.WAKE_MODEL_POWER], inference_framework="onnx")
