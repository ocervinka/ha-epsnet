from ..models import MathFormula, ModuleChannel, ModuleItem, RuntimeModelInfo, value_item

MODEL_INFO = RuntimeModelInfo(
    unit_code=0x0D31,
    manufacturer="Teco",
    model="C-RC-0011R",
    type_key="generic_room_controller",
    subsystem="room_controls",
    description="Wall room controller: temperature, humidity, external temperature, buttons",
    # Inputs: buttons, then (if enabled in the program) the internal thermometer with
    # humidity and the external thermometer; internal display data follows.
    inputs=(
        ModuleItem(
            tuple(
                ModuleChannel(key=f"button_{i}", label=f"Button {i}", role="button", bit=i - 1)
                for i in range(1, 6)
            )
        ),
        value_item("temperature", "Temperature", "temperature", MathFormula.DIV_10, optional=True),
        value_item("humidity", "Humidity", "humidity", optional=True, byte_value=True),
        value_item(
            "external_temperature", "External temperature", "temperature", MathFormula.DIV_10,
            optional=True,
        ),
    ),
    # outputs are the display contents the program writes: not described
    extra_items_ignored=True,
)
