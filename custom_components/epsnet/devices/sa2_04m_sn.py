from ..models import RuntimeModelInfo, bit_item

MODEL_INFO = RuntimeModelInfo(
    unit_code=0x0C32,
    manufacturer="ELKO EP / Teco",
    model="SA2-04M/Sn",
    type_key="generic_relay_module",
    subsystem="relay_outputs",
    description="4-channel relay actuator",
    inputs=(bit_item("local_switch", ("SW1", "SW2", "SW3", "SW4"), key_prefix="local"),),
    outputs=(bit_item("relay", ("RE1", "RE2", "RE3", "RE4"), key_prefix="relay"),),
)
