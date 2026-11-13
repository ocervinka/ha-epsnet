from ..models import RuntimeModelInfo, bit_item, value_item, MathFormula

MODEL_INFO = RuntimeModelInfo(
    unit_code=0x0C35,
    manufacturer="ELKO EP / Teco",
    model="WSB2-20",
    type_key="generic_room_controller",
    subsystem="room_controls",
    description="2-button wall switch with LEDs and thermostat input",
    inputs=(
        bit_item("button", ("UP1", "DOWN1"), key_prefix="button"),
        value_item("temperature", "Temperature", "temperature", MathFormula.DIV_100),
    ),
    outputs=(bit_item("led", ("GREEN1", "RED1"), key_prefix="led"),),
)
