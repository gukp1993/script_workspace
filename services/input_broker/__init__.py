"""input_broker——输入代理服务（E08）。

INP-001：InputIntent / InputBatch 与过期语义；
INP-002：FakeInputSink（绝不调用系统输入）与 RealInputSink 占位；
INP-003：SendInputAdapter 唯一真实输入出口（前台扫描码/绝对坐标）；
INP-004：KeyLedger 按键账本与幂等释放；
INP-005：InputBroker 可注入前台复核（真实实现由 window_service 提供）；
INP-006：EstopHotkey 全局急停热键 + EstopController 两步急停；
INP-007：ParentWatchdog 父进程看门狗；
INP-009：取消屏障（epoch，取消后旧批次不得再执行）；
INP-010：AuditLogger 安全事件审计（policy_decision/estop/anomaly）。

安全边界：FakeInputSink 与真实适配器都通过 InputSink 协议注入；
win32/ctypes 调用只存在于 win32_adapter / win32_hotkey / win32_watchdog
三个文件（静态守卫约定），其余模块一律面向抽象接口。
"""

from input_broker.audit import AuditLogger
from input_broker.broker import InputBroker, REAL_INPUT_MODE
from input_broker.estop import EstopController, EstopRecord
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
from input_broker.win32_adapter import (
    ABSOLUTE_MAX,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    KEYEVENTF_SCANCODE,
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_WHEEL,
    ScanCodes,
    SendInputAdapter,
    SendInputUnit,
)
from input_broker.win32_hotkey import EstopHotkey, HotkeyCombo, HotkeyRegistrar
from input_broker.win32_watchdog import ParentWatchdog, probe_alive_default

__all__ = [
    "ABSOLUTE_MAX",
    "DEFAULT_BATCH_TTL_MS",
    "DEFAULT_INTENT_TTL_MS",
    "AuditLogger",
    "EstopController",
    "EstopHotkey",
    "EstopRecord",
    "FakeInputSink",
    "HotkeyCombo",
    "HotkeyRegistrar",
    "InputBatch",
    "InputBroker",
    "InputIntent",
    "InputSink",
    "IntentKind",
    "KEYEVENTF_EXTENDEDKEY",
    "KEYEVENTF_KEYUP",
    "KEYEVENTF_SCANCODE",
    "KeyLedger",
    "MOUSEEVENTF_ABSOLUTE",
    "MOUSEEVENTF_LEFTDOWN",
    "MOUSEEVENTF_LEFTUP",
    "MOUSEEVENTF_MOVE",
    "MOUSEEVENTF_WHEEL",
    "PATH_EXECUTE",
    "PATH_SHADOW",
    "ParentWatchdog",
    "REAL_INPUT_MODE",
    "RealInputSink",
    "ScanCodes",
    "SendInputAdapter",
    "SendInputUnit",
    "SinkContext",
    "SinkRecord",
    "SinkRejection",
    "SinkResult",
    "WAIT_KIND",
    "make_batch",
    "make_intent",
    "normalize_key",
    "probe_alive_default",
]
