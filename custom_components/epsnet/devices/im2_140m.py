from ..models import ModuleChannel, ModuleItem, RuntimeModelInfo

MODEL_INFO = RuntimeModelInfo(
    unit_code=0x0C25,
    manufacturer="ELKO EP / Teco",
    model="IM2-140M",
    type_key="generic_input_module",
    subsystem="digital_inputs",
    description="14-channel digital input module",
    # one 24-bit item: inputs in bits 0-13, tamper contacts in bits 17-23
    inputs=(
        ModuleItem(
            tuple(
                ModuleChannel(key=f"input_in{i}", label=f"IN{i}", role="input", bit=i - 1)
                for i in range(1, 15)
            )
            + tuple(
                ModuleChannel(key=f"tamper_{i}", label=f"TAMPER{i}", role="tamper", bit=16 + i)
                for i in range(1, 8)
            )
        ),
    ),
)
