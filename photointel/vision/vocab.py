"""Zero-shot tag vocabulary. Each tag = (name, category, prompts).

Scores come from the semantic model's text/image similarity; thresholds are
calibrated per category (see eval/calibrate_tags.py). The vocabulary version is
stored so tags are recomputed when it changes.
"""
from __future__ import annotations

VOCAB_VERSION = 4

# category -> list of (tag, [prompts])
VOCAB: dict[str, list[tuple[str, list[str]]]] = {
    "event": [
        ("wedding", ["a photo of a wedding ceremony", "a bride and groom at their wedding", "an indian wedding ceremony with a bride in a red saree"]),
        ("birthday", ["a birthday party with a birthday cake and candles", "people celebrating a birthday"]),
        ("party", ["people at a party", "a festive celebration with decorations"]),
        ("festival", ["a festival celebration", "diwali celebration with oil lamps and fireworks", "people celebrating holi with colored powder"]),
        ("religious ceremony", ["a hindu puja ceremony", "a religious ceremony", "people praying at a religious ritual"]),
        ("mehendi", ["henna mehendi designs on hands", "a mehendi or haldi ceremony"]),
        ("graduation", ["a graduation ceremony with caps and gowns"]),
        ("concert", ["a live music concert with a stage and crowd"]),
        ("sports event", ["a sports match in a stadium", "people playing cricket", "a football match"]),
        ("family gathering", ["a family gathering at home", "a group of family members posing together"]),
        ("dinner", ["people having dinner together at a table", "a group meal at a restaurant"]),
        ("picnic", ["a picnic outdoors on the grass"]),
        ("conference", ["a conference or presentation in a hall", "a business meeting"]),
        ("school event", ["a school function with children on stage", "students in a classroom"]),
        ("baby shower", ["a baby shower celebration", "a baby naming ceremony"]),
        ("road trip", ["a road trip view through a car window", "a car on a highway during travel"]),
    ],
    "scene": [
        ("beach", ["a photo of a beach", "people at the beach with sea waves"]),
        ("mountains", ["a photo of mountains", "a mountain landscape"]),
        ("snow", ["a snowy landscape", "people playing in the snow"]),
        ("forest", ["a photo of a forest with trees"]),
        ("lake", ["a photo of a lake"]),
        ("river", ["a photo of a river"]),
        ("waterfall", ["a photo of a waterfall"]),
        ("desert", ["a desert with sand dunes"]),
        ("countryside", ["a rural countryside with farm fields", "a village in the countryside"]),
        ("park", ["a city park with grass and trees"]),
        ("garden", ["a garden with flowers and plants"]),
        ("city street", ["a busy city street", "an urban street with buildings"]),
        ("cityscape", ["a city skyline", "an aerial view of a city"]),
        ("night", ["a photo taken at night", "city lights at night"]),
        ("sunset", ["a sunset", "a sunrise over the horizon"]),
        ("temple", ["a hindu temple", "a temple with a gopuram tower"]),
        ("church", ["a church"]),
        ("mosque", ["a mosque"]),
        ("monument", ["a historic monument", "an ancient fort or palace"]),
        ("museum", ["inside a museum"]),
        ("restaurant", ["inside a restaurant", "a cafe interior"]),
        ("home interior", ["a living room at home", "inside a house"]),
        ("kitchen", ["a kitchen"]),
        ("office", ["an office with desks and computers"]),
        ("classroom", ["a classroom"]),
        ("shopping", ["a shopping mall", "a market with shops and stalls"]),
        ("airport", ["an airport terminal", "an airplane"]),
        ("train station", ["a railway station with a train"]),
        ("stadium", ["a stadium"]),
        ("swimming pool", ["a swimming pool"]),
        ("hotel", ["a hotel room", "a hotel lobby"]),
        ("stage", ["people performing on a stage"]),
        ("zoo", ["animals at a zoo"]),
        ("amusement park", ["an amusement park with rides"]),
        ("underwater", ["an underwater photo"]),
        ("aerial view", ["an aerial drone photo"]),
    ],
    "object": [
        ("cake", ["a photo of a cake"]),
        ("food", ["a plate of food", "a delicious meal"]),
        ("indian food", ["indian food like biryani, dosa or thali"]),
        ("drinks", ["drinks and beverages", "a cup of coffee or tea"]),
        ("flowers", ["a photo of flowers", "a flower garland"]),
        ("car", ["a photo of a car"]),
        ("motorcycle", ["a motorcycle or scooter"]),
        ("bicycle", ["a bicycle"]),
        ("boat", ["a boat on the water"]),
        ("train", ["a train"]),
        ("dog", ["a photo of a dog"]),
        ("cat", ["a photo of a cat"]),
        ("bird", ["a photo of a bird"]),
        ("cow", ["a cow"]),
        ("elephant", ["an elephant"]),
        ("horse", ["a horse"]),
        ("baby", ["a baby", "an infant"]),
        ("children", ["children playing", "kids"]),
        ("group photo", ["a group photo of many people posing", "a large group of people"]),
        ("couple", ["a couple posing together"]),
        ("selfie", ["a selfie taken with a phone camera"]),
        ("portrait", ["a portrait photo of a person"]),
        ("document", ["a document with printed text", "a scanned paper document"]),
        ("receipt", ["a receipt or bill"]),
        ("id card", ["an identity card"]),
        ("whiteboard", ["a whiteboard with writing"]),
        ("screenshot", ["a screenshot of a phone screen", "a screenshot of an app or website"]),
        ("meme", ["an internet meme with text"]),
        ("book", ["a book"]),
        ("laptop", ["a laptop computer"]),
        ("balloons", ["balloons"]),
        ("gifts", ["gift boxes and presents"]),
        ("decorations", ["decorative lights and decorations"]),
        ("fireworks", ["fireworks in the sky"]),
        ("rangoli", ["a colorful rangoli design on the floor"]),
        ("traditional attire", ["people wearing traditional indian clothes like saree or kurta"]),
        ("christmas tree", ["a christmas tree"]),
        ("artwork", ["a painting or artwork"]),
        ("vehicle interior", ["inside a car"]),
        ("statue", ["a statue or idol"]),
    ],
    "activity": [
        ("dancing", ["people dancing"]),
        ("swimming", ["people swimming"]),
        ("hiking", ["people hiking on a trail"]),
        ("cycling", ["people riding bicycles"]),
        ("cooking", ["a person cooking"]),
        ("eating", ["people eating food"]),
        ("playing", ["people playing a game"]),
        ("cricket", ["playing cricket"]),
        ("football", ["playing football or soccer"]),
        ("praying", ["people praying"]),
        ("traveling", ["tourists sightseeing", "travelers with luggage"]),
        ("camping", ["camping with a tent"]),
        ("boating", ["people on a boat ride"]),
        ("performing", ["a musical or dance performance"]),
        ("skiing", ["people skiing"]),
        ("surfing", ["people surfing"]),
    ],
}

AESTHETIC_POSITIVE = [
    "a beautiful, high quality photograph",
    "a stunning, well-composed professional photo",
    "an award-winning photograph with great lighting",
]
AESTHETIC_NEGATIVE = [
    "a blurry, low quality photo",
    "a boring, badly composed amateur snapshot",
    "a dark, noisy, poorly lit photo",
]

# Event categories usable for event titles, in priority order when scores tie.
EVENT_TITLE_CATEGORIES = [t for t, _ in VOCAB["event"]] + ["beach", "mountains", "snow", "temple", "monument", "waterfall", "lake"]


def all_tags() -> list[tuple[str, str, list[str]]]:
    return [(tag, cat, prompts) for cat, items in VOCAB.items() for tag, prompts in items]
