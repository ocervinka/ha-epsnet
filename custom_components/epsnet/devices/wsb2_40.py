from ..models import RuntimeModelInfo, bit_item, value_item, MathFormula

MODEL_INFO = RuntimeModelInfo(
    unit_code=0x0C36,
    manufacturer="ELKO EP / Teco",
    model="WSB2-40",
    type_key="generic_room_controller",
    subsystem="room_controls",
    description="4-button wall switch with LEDs and thermostat input",
    inputs=(
        bit_item("button", ("UP1", "DOWN1", "UP2", "DOWN2"), key_prefix="button"),
        value_item("temperature", "Temperature", "temperature", MathFormula.DIV_100),
    ),
    outputs=(
        bit_item("led", ("GREEN1", "RED1", "GREEN2", "RED2"), key_prefix="led"),
    ),
)
