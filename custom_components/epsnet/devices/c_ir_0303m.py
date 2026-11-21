from ..models import MathFormula, ModuleChannel, ModuleItem, RuntimeModelInfo, value_item

_STATUS = ("OUF1", "VLD1", "OUF2", "VLD2", "OUF3", "VLD3", "MAN")

MODEL_INFO = RuntimeModelInfo(
    unit_code=0x0D20,
    manufacturer="Teco",
    model="C-IR-0303M",
    # the kind that may carry inputs, outputs and analog values together
    type_key="generic_room_controller",
    subsystem="room_controls",
    description="3 digital / analog inputs and 3 relay outputs",
    # From the manufacturer's description of the module; not tried on one yet.
    # Each part can be switched off in the program, so every item is optional.
    inputs=(
        # one 16-bit item: the inputs, a short and a long press of each, tampers
        ModuleItem(
            tuple(
                ModuleChannel(key=f"input_di{i}", label=f"DI{i}", role="input", bit=i - 1)
                for i in range(1, 4)
            )
            + tuple(
                ModuleChannel(key=f"input_click{i}", label=f"CLICK{i}", role="input", bit=3 + i)
                for i in range(1, 4)
            )
            + tuple(
                ModuleChannel(key=f"input_press{i}", label=f"PRESS{i}", role="input", bit=7 + i)
                for i in range(1, 4)
            )
            + tuple(
                ModuleChannel(key=f"tamper_{i}", label=f"TAMPER{i}", role="tamper", bit=11 + i)
                for i in range(1, 4)
            ),
            optional=True,
        ),
        ModuleItem(
            tuple(
                ModuleChannel(key=f"status_{label.lower()}", label=label, role="status", bit=bit)
                for bit, label in enumerate(_STATUS)
            ),
            optional=True,
        ),
        value_item("analog_1", "AI1", "analog_temperature", MathFormula.DIV_10, optional=True),
        value_item("analog_2", "AI2", "analog_temperature", MathFormula.DIV_10, optional=True),
        value_item("analog_3", "AI3", "analog_temperature", MathFormula.DIV_10, optional=True),
    ),
    outputs=(
        ModuleItem(
            tuple(
                ModuleChannel(key=f"relay_do{i}", label=f"DO{i}", role="relay", bit=i - 1)
                for i in range(1, 4)
            ),
            optional=True,
        ),
    ),
)
