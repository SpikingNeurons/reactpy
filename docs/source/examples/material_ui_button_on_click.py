import json

import idom


material_ui = idom.install("@material-ui/core")
MaterialButton = material_ui.define("Button", fallback="loading...")


@idom.element
def ViewSliderEvents():
    event, set_event = idom.hooks.use_state(None)

    return idom.html.div(
        MaterialButton(
            {
                "color": "primary",
                "variant": "contained",
                "onClick": lambda event: set_event(event),
            },
            "Click Me!",
        ),
        idom.html.pre(json.dumps(event, indent=2)),
    )


idom.run(ViewSliderEvents)