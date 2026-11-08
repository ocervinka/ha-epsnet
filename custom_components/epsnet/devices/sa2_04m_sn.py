from ..models import RuntimeChannelDefinition, RuntimeModelInfo

MODEL_INFO = RuntimeModelInfo(
    unit_code=0x0C32,
    manufacturer="ELKO EP / Teco",
    model="SA2-04M/Sn",
    type_key="generic_relay_module",
    subsystem="relay_outputs",
    description="4-channel relay actuator",
    channels=(
        *(
            RuntimeChannelDefinition(
                role="local_switch",
                name_template="local_switch_{channel:02d}",
                channel_index=index + 1,
                register="X",
                storage="B",
                data_type="BOOL",
                address_offset=0,
                bit_offset=index,
            )
            for index in range(4)
        ),
        *(
            RuntimeChannelDefinition(
                role="channel_state",
                name_template="relay_{channel:02d}",
                channel_index=index + 1,
                register="Y",
                storage="B",
                data_type="BOOL",
                address_offset=0,
                bit_offset=index,
                writable=True,
            )
            for index in range(4)
        ),
    ),
)
