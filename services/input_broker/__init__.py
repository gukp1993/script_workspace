"""input_broker——输入代理服务（E08）。

INP-001：InputIntent / InputBatch 与过期语义；
INP-002：FakeInputSink（绝不调用系统输入）与 RealInputSink 占位；
INP-004：KeyLedger 按键账本与幂等释放；
InputBroker：策略决策之后的最后一道执行闸。
"""

from input_broker.broker import InputBroker, REAL_INPUT_MODE
from input_broker.fake_sink import (
    FakeInputSink,
    InputSink,
    PATH_EXECUTE,
    PATH_SHADOW,
    RealInputSink,
    SinkContext,
    SinkRecord,
    SinkRejection,
    SinkResult,
)
from input_broker.intents import (
    DEFAULT_BATCH_TTL_MS,
    DEFAULT_INTENT_TTL_MS,
    InputBatch,
    InputIntent,
    IntentKind,
    WAIT_KIND,
    make_batch,
    make_intent,
    normalize_key,
)
from input_broker.key_ledger import KeyLedger

__all__ = [
    "DEFAULT_BATCH_TTL_MS",
    "DEFAULT_INTENT_TTL_MS",
    "FakeInputSink",
    "InputBatch",
    "InputBroker",
    "InputIntent",
    "InputSink",
    "IntentKind",
    "KeyLedger",
    "PATH_EXECUTE",
    "PATH_SHADOW",
    "REAL_INPUT_MODE",
    "RealInputSink",
    "SinkContext",
    "SinkRecord",
    "SinkRejection",
    "SinkResult",
    "WAIT_KIND",
    "make_batch",
    "make_intent",
    "normalize_key",
]
