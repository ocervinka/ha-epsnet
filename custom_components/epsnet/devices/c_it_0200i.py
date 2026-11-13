from ..models import RuntimeModelInfo, bit_item, value_item, MathFormula

MODEL_INFO = RuntimeModelInfo(
    unit_code=0x0CED,
    manufacturer="Teco",
    model="C-IT-0200I",
    type_key="generic_analog_sensor_module",
    subsystem="analog_sensors",
    description="2-channel temperature interface (thermocouples) with its own thermometer",
    inputs=(
        bit_item(
            "status", ("OUF1", "VLD1", "OUF2", "VLD2", "hOUF", "hVLD"), key_prefix="status"
        ),
        value_item("analog_1", "AI1", "analog_temperature", MathFormula.DIV_10),
        value_item("analog_2", "AI2", "analog_temperature", MathFormula.DIV_10),
        value_item("internal_temperature", "hTHERM", "temperature", MathFormula.DIV_100),
    ),
)
