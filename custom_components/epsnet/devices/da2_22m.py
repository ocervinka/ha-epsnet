from ..models import RuntimeChannelDefinition, RuntimeModelInfo

MODEL_INFO = RuntimeModelInfo(
    unit_code=0x0C1D,
    manufacturer="ELKO EP / Teco",
    model="DA2-22M",
    type_key="generic_dimmer_module",
    subsystem="dimmers",
    description="2-channel dimmer actuator",
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
                role="local_switch",
                name_template="local_switch_{channel:02d}",
                channel_index=index + 1,
                register="X",
                storage="B",
                data_type="BOOL",
                address_offset=1,
                bit_offset=index,
            )
            for index in range(2)
        ),
        *(
            RuntimeChannelDefinition(
                role="channel_state",
                name_template="dimmer_{channel:02d}",
                channel_index=index + 1,
                register="Y",
                storage="F",
                data_type="REAL",
                address_offset=index * 4,
                writable=True,
            )
            for index in range(2)
        ),
    ),
)
