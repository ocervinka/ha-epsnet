from ..models import RuntimeModelInfo, bit_item

MODEL_INFO = RuntimeModelInfo(
    unit_code=0x0C4F,
    manufacturer="ELKO EP / Teco",
    model="SA2-012M",
    type_key="generic_relay_module",
    subsystem="relay_outputs",
    description="12-channel relay actuator",
    inputs=(
        bit_item("local_switch", tuple(f"SW{i}" for i in range(1, 13)), key_prefix="local"),
    ),
    outputs=(bit_item("relay", tuple(f"RE{i}" for i in range(1, 13)), key_prefix="relay"),),
)
