DEFAULT_SIZE_TEMPLATE_PRODUCT = (
    "Personalized Wedding Guest Book – Linen Photo Album, "
    "Instax Polaroid Compatible"
)

DEFAULT_SIZE_TEMPLATE_OPTIONS = [
    {
        "id": "9x6",
        "label": "9*6",
        "size_unit": "in",
        "single_side_width": 12.01,
        "single_side_height": 8.58,
        "bleed": 0.24,
        "spine_width": 0.65,
        "spine_bleed": 0.12,
    },
    {
        "id": "10x12",
        "label": "10*12",
        "size_unit": "in",
        "single_side_width": 0,
        "single_side_height": 0,
        "bleed": 0,
        "spine_width": 0,
        "spine_bleed": 0,
    },
    {
        "id": "12x12",
        "label": "12*12",
        "size_unit": "in",
        "single_side_width": 0,
        "single_side_height": 0,
        "bleed": 0,
        "spine_width": 0,
        "spine_bleed": 0,
    },
]

DEFAULT_SIZE_TEMPLATE_FIELDS = {
    "page_count": 80,
    "page_count_arr": [80],
    "size_spec": {
        "selected": "9x6",
        "display_unit": "in",
        "options": DEFAULT_SIZE_TEMPLATE_OPTIONS,
    },
}
