from ..models import RuntimeModelInfo, bit_item, ModuleItem, ModuleChannel

MODEL_INFO = RuntimeModelInfo(
    unit_code=0x0C1D,
    manufacturer="ELKO EP / Teco",
    model="DA2-22M",
    type_key="generic_dimmer_module",
    subsystem="dimmers",
    description="2-channel dimmer actuator",
    inputs=(
        bit_item("input", ("IN1", "IN2"), key_prefix="input"),
        bit_item("local_switch", ("SW1", "SW2"), key_prefix="local"),
    ),
    outputs=(
        ModuleItem((ModuleChannel(key="dimmer_out1", label="OUT1", role="dimmer"),)),
        ModuleItem((ModuleChannel(key="dimmer_out2", label="OUT2", role="dimmer"),)),
    ),
)
