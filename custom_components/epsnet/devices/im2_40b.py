from ..models import ModuleChannel, ModuleItem, RuntimeModelInfo

MODEL_INFO = RuntimeModelInfo(
    unit_code=0x0C4E,
    manufacturer="ELKO EP / Teco",
    model="IM2-40B",
    type_key="generic_input_module",
    subsystem="digital_inputs",
    description="4-channel digital input module",
    # one 16-bit item: inputs in bits 0-3, tamper contacts in bits 8-9
    inputs=(
        ModuleItem(
            tuple(
                ModuleChannel(key=f"input_in{i}", label=f"IN{i}", role="input", bit=i - 1)
                for i in range(1, 5)
            )
            + tuple(
                ModuleChannel(key=f"tamper_{i}", label=f"TAMPER{i}", role="tamper", bit=7 + i)
                for i in range(1, 3)
            )
        ),
    ),
)
