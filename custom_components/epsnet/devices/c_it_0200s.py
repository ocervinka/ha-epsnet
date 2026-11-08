from ..models import MathFormula, RuntimeChannelDefinition, RuntimeModelInfo

MODEL_INFO = RuntimeModelInfo(
    unit_code=0x0CED,
    manufacturer="Teco",
    model="C-IT-0200S",
    type_key="generic_analog_sensor_module",
    subsystem="analog_sensors",
    description="Analog temperature and binary input interface",
    channels=(
        *(
            RuntimeChannelDefinition(
                role="binary_input",
                name_template="input_{channel:02d}",
                channel_index=index + 1,
                register="X",
                storage="B",
                data_type="BOOL",
                address_offset=0,
                bit_offset=index,
            )
            for index in range(2)
        ),
        *(
            RuntimeChannelDefinition(
                role="real_value",
                name_template="value_{channel:02d}",
                channel_index=index + 1,
                register="X",
                storage="F",
                data_type="REAL",
                address_offset=4 + (index * 4),
                transform_read=MathFormula.DIV_10,
            )
            for index in range(2)
        ),
    ),
)
