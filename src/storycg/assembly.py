"""Deterministic prompt construction from locked identity and shot state."""


def assemble_prompt(identity: dict, entry: dict) -> dict:
    identity_block = identity["identity_block"]
    outfit = entry["outfit"]
    scene = entry["scene"]
    shot = entry["shot"]
    outfit_block = outfit["clothing_state"]
    scene_block = ", ".join(filter(None, [scene["location"], scene["time"],
                                           scene["weather"], scene["lighting"],
                                           scene["environment_change"]]))
    props = [f'{item["name"]} held by {item["held_by"]}, {item["position"]}'
             for item in shot["key_props"]]
    shot_block = ", ".join(filter(None, [shot["action"], shot["expression"],
                                          *props, shot["spatial_relation"],
                                          shot["camera_angle"], shot["framing"] + " shot",
                                          shot["change_from_previous"]]))
    return {"identity_block": identity_block, "outfit_block": outfit_block,
            "scene_block": scene_block, "shot_block": shot_block,
            "positive_tags": ", ".join([identity_block, outfit_block, scene_block, shot_block]),
            "framing": shot["framing"]}
