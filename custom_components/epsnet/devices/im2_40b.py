from ..models import RuntimeChannelDefinition, RuntimeModelInfo

MODEL_INFO = RuntimeModelInfo(
    unit_code=0x0C4E,
    manufacturer="ELKO EP / Teco",
    model="IM2-40B",
    type_key="generic_input_module",
    subsystem="digital_inputs",
    description="4-channel digital input module",
    channels=tuple(
        RuntimeChannelDefinition(
            role="input_channel",
            name_template="input_{channel:02d}",
            channel_index=index + 1,
            register="X",
            storage="B",
            data_type="BOOL",
            address_offset=0,
            bit_offset=index,
        )
        for index in range(4)
    ),
)
