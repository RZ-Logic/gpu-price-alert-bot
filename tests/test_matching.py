"""Truth table for gpu_alerts.matching: the traps that broke v1, plus vendor part codes.

Titles are real retailer titles from the 2026-10-12 audit or close variants of them.
All offline.
"""

import pytest

from gpu_alerts import catalog
from gpu_alerts.matching import Kind, MatchResult, ParsedTitle, match, normalise, parse_title


def m(model_id: str):
    model = catalog.by_id(model_id)
    assert model is not None, model_id
    return model


# (title, model id, expected ok, expected reason or a prefix of it)
MATCH_CASES = [
    # "TI" inside MULTI and EDITION: v1 accepted these for "RTX 5070 TI".
    ("ASUS Prime GeForce RTX 5070 12GB GDDR7 Multi-Display OC", "rtx-5070-ti", False, "wrong model: RTX 5070"),
    ("ASUS Prime GeForce RTX 5070 12GB GDDR7 Multi-Display OC", "rtx-5070", True, "match"),
    ("ASUS Prime GeForce RTX 5070 12GB GDDR7 OC Edition", "rtx-5070-ti", False, "wrong model: RTX 5070"),
    ("ASUS GeForce RTX 5060 LP BRK 8GB GDDR7 OC Edition Graphics Card", "rtx-5060-ti-8gb", False, "wrong model: RTX 5060"),
    ("ASUS GeForce RTX 5060 LP BRK 8GB GDDR7 OC Edition Graphics Card", "rtx-5060", True, "match"),
    # "XT" inside NEXT.
    ("Sapphire Pulse Radeon RX 7600 8GB GDDR6 Next-Gen Gaming Graphics Card", "rx-7600-xt", False, "wrong model: RX 7600"),
    ("Sapphire Pulse Radeon RX 7600 8GB GDDR6 Next-Gen Gaming Graphics Card", "rx-7600", True, "match"),
    # XTX vs XT.
    ("XFX Speedster MERC310 AMD Radeon RX 7900 XTX 24GB GDDR6", "rx-7900-xt", False, "wrong model: RX 7900 XTX"),
    ("XFX Speedster MERC310 AMD Radeon RX 7900 XTX 24GB GDDR6", "rx-7900-xtx", True, "match"),
    ("PowerColor Hellhound Radeon RX 7900 XT 20GB GDDR6", "rx-7900-xtx", False, "wrong model: RX 7900 XT"),
    # GRE and XT are not the base RX 9070.
    ("GIGABYTE Gaming Radeon RX 9070 GRE Graphics Card GV-R907GREGAMING OC-12GD", "rx-9070", False, "wrong model: RX 9070 GRE"),
    ("GIGABYTE Gaming Radeon RX 9070 GRE Graphics Card GV-R907GREGAMING OC-12GD", "rx-9070-gre", True, "match"),
    ("ASUS Prime Radeon RX 9070 GRE OC Edition Gaming Graphics Card (AMD Radeon RX 9070, PCIe 5.0)", "rx-9070", False, "wrong model: RX 9070 GRE"),
    ("ASRock Steel Legend Radeon RX 9070 XT 16GB GDDR6 Graphics Card RX9070XT SL 16G", "rx-9070", False, "wrong model: RX 9070 XT"),
    ("ASRock Challenger Radeon RX 9070 Graphics Card RX9070 CL 16G", "rx-9070", True, "match"),
    ("ASRock Challenger Radeon RX 9070 Graphics Card RX9070 CL 16G", "rx-9070-xt", False, "wrong model: RX 9070"),
    ("ASUS Prime Radeon RX 9070 EVO 16GB GDDR6 OC Edition Gaming Graphics Card", "rx-9070", True, "match"),
    # SUPER and TI SUPER: most specific wins, suffix must be exact.
    ("MSI GeForce RTX 4070 SUPER 12G VENTUS 2X OC", "rtx-4070", False, "wrong model: RTX 4070 SUPER"),
    ("MSI GeForce RTX 4070 SUPER 12G VENTUS 2X OC", "rtx-4070-super", True, "match"),
    ("MSI GeForce RTX 4070 SUPER 12G VENTUS 2X OC", "rtx-4070-ti", False, "wrong model: RTX 4070 SUPER"),
    ("ASUS TUF Gaming GeForce RTX 4070 Ti SUPER 16GB GDDR6X", "rtx-4070-ti", False, "wrong model: RTX 4070 TI SUPER"),
    ("ASUS TUF Gaming GeForce RTX 4070 Ti SUPER 16GB GDDR6X", "rtx-4070-ti-super", True, "match"),
    ("ASUS TUF Gaming GeForce RTX 4070 Ti SUPER 16GB GDDR6X", "rtx-4070-super", False, "wrong model: RTX 4070 TI SUPER"),
    ("ASUS TUF-RTX4070TIS-O16G-GAMING", "rtx-4070-ti-super", True, "match"),
    # China variants and workstation cards are other products.
    ("Colorful IGame RTX 5090 D v2 Advanced OC 24GB PCIE 5.0 GPU Graphics Card", "rtx-5090", False, "wrong model: RTX 5090 D V2"),
    ("ASUS ROG Astral BTF GeForce RTX 5090 24GB GDDR7 BTF OC Edition ROG-ASTRAL-RTX5090DV2-O24G-BTF", "rtx-5090", False, "wrong model: RTX 5090 D V2"),
    ("MSI GeForce RTX 5090D 32G SUPRIM SOC", "rtx-5090", False, "wrong model: RTX 5090 D"),
    ("PNY NVIDIA RTX PRO 5000 Blackwell Graphic Card 48 GB GDDR7 ECC", "rtx-5080", False, "wrong model: RTX PRO 5000"),
    ("Sparkle Intel ArcPro B60 Blower, 24GB GDDR6, Metal Backplate, SBP60W-24G", "arc-b580", False, "wrong model: ARC PRO B60"),
    ("INTEL ARC PRO B70 32GB GDDR6", "arc-b580", False, "wrong model: ARC PRO B70"),
    ("Sparkle Intel Arc B570 Guardian OC Graphics Card, 10GB GDDR6", "arc-b580", False, "wrong model: ARC B570"),
    ("ASRock Challenger Intel ARC A580 CL 8G GDDR6", "arc-b580", False, "wrong model: ARC A580"),
    # SKU-code-only titles.
    ("GIGABYTE GV-N507TGAMING OC-16GD", "rtx-5070-ti", True, "match"),
    ("GIGABYTE GV-N507TGAMING OC-16GD", "rtx-5070", False, "wrong model: RTX 5070 TI"),
    ("GIGABYTE GV-N5070GAMING OC-12GD", "rtx-5070", True, "match"),
    ("GIGABYTE GVN5070AERO OC12GD GeForce RTX 5070 AERO OC 12GB Graphics Card", "rtx-5070", True, "match"),
    ("GIGABYTE GV-N506TEAGLE OC-8GD GDDR7 Double Fans", "rtx-5060-ti-8gb", True, "match"),
    ("GIGABYTE GV-N506TEAGLE OC-8GD GDDR7 Double Fans", "rtx-5060-ti-16gb", False, "vram 8 != 16"),
    ("GIGABYTE GV-N506TGAMING OC-16GD GDDR7 Triple Fans", "rtx-5060-ti-16gb", True, "match"),
    ("GIGABYTE GV-R9060XTGAMING OC-16GD", "rx-9060-xt-16gb", True, "match"),
    ("GIGABYTE Gaming Radeon RX 9070 XT Graphics Card GV-R907XGAMINGOCICE-16GD", "rx-9070-xt", True, "match"),
    ("ASUS TUF-RTX5060TI-O16G-GAMING", "rtx-5060-ti-16gb", True, "match"),
    ("ASUS PRIME-RTX5070-O12G", "rtx-5070", True, "match"),
    ("ASUS PRIME-RTX5070-O12G", "rtx-5070-ti", False, "wrong model: RTX 5070"),
    ("MSI G506T16V2CP RTX 5060 Ti 16GB Ventus 2X OC Plus Graphics Card", "rtx-5060-ti-16gb", True, "match"),
    ("MSI G5070-12S2C GDDR7 Double Fans(Open Box)", "rtx-5070", True, "match"),
    ("PNY VCG5070T16TFXXPB1-O GDDR7 Graphics Card", "rtx-5070-ti", True, "match"),
    ("ASRock America VG-B580CL 12GO 12GB OC Graphics Card", "arc-b580", True, "match"),
    ("Sparkle SB580T-12G OC Graphics Card", "arc-b580", True, "match"),
    ("Sapphire 11348-01-20G Nitro+ Gaming OC Graphics Card", "rx-9070-xt", True, "match"),
    ("XFX RX-97TMARGBA Mercury Magnetic Air Graphics Card", "rx-9070-xt", True, "match"),
    # VRAM rule: precision first.
    ("MSI GeForce RTX 5060 Ti 8G VENTUS 2X OC PLUS GDDR7 Double Fans", "rtx-5060-ti-16gb", False, "vram 8 != 16"),
    ("MSI GeForce RTX 5060 Ti 8G VENTUS 2X OC PLUS GDDR7 Double Fans", "rtx-5060-ti-8gb", True, "match"),
    ("ZOTAC GAMING GeForce RTX 5060 Ti Twin Edge OC White Edition GDDR7", "rtx-5060-ti-16gb", False, "vram unknown"),
    ("Sapphire Pulse AMD Radeon RX 9060 XT 8GB GDDR6 Gaming GPU", "rx-9060-xt-16gb", False, "vram 8 != 16"),
    ("ASUS PRIME Radeon RX 907016GB 256-Bit GDDR6 PCI Express 5.0 Graphics Card PRIME-RX9070-O16G", "rx-9070", True, "match"),
    # v1 junk-word false negatives: FAN, RAM in FRAME, STAND in STANDARD, ARC in ARCHITECTURE.
    ("PNY GeForce RTX 5080 Triple Fan", "rtx-5080", True, "match"),
    ("ASUS Prime Radeon RX 9070 OC GDDR6 Triple Fans", "rx-9070", True, "match"),
    ("ASUS ROG Astral GeForce RTX 5090 32GB GDDR7 (Four Fans, 3.8 Slot Design)", "rtx-5090", True, "match"),
    ("MSI GeForce RTX 5070 12G Ventus 2X OC, DLSS 4 Multi Frame Generation", "rtx-5070", True, "match"),
    ("NVIDIA GeForce RTX 5070 Founders Edition Standard Edition 12GB", "rtx-5070", True, "match"),
    ("msi Gaming RTX 5080 16G Inspire 3X OC (16GB GDDR7, NVIDIA Blackwell Architecture)", "rtx-5080", True, "match"),
    ("ASUS ProArt GeForce RTX 5080 OC Edition Graphics Card (16GB GDDR7, Axial-tech Fans, Phase-change GPU Thermal Pad)", "rtx-5080", True, "match"),
    # Laptops, mobile parts and prebuilts.
    ('ASUS ROG Strix G16 16" Intel Ultra 9-275HX RTX 5070 Ti 32GB 1TB Win11H', "rtx-5070-ti", False, "laptop"),
    ("Razer Blade 16 (2026) Gaming Laptop, RTX 5090, 32GB DDR5, 2TB, Black", "rtx-5090", False, "laptop"),
    ("NVIDIA GeForce RTX 5070 Ti Laptop GPU 12GB", "rtx-5070-ti", False, "laptop"),
    ("ARMOURY AMD Ryzen 7 9800X3D GeForce RTX 5080 32GB RAM 2TB SSD W11P", "rtx-5080", False, "prebuilt"),
    ("CyberPowerPC Gaming PC, AMD Ryzen 7 8700F, GeForce RTX 5060 Ti 8GB", "rtx-5060-ti-8gb", False, "prebuilt"),
    ("Acer Nitro 60 AMD R7 8700F RTX 5070 32GB RAM 2TB SSD Win11H", "rtx-5070", False, "prebuilt"),
    ("ROG NUC Intel Core Ultra 9 275HX RTX 5070Ti 32GB RAM 2TB SSD Win11H", "rtx-5070-ti", False, "prebuilt"),
    # Accessories, eGPUs and bundles.
    ("EK-Quantum Vector2 Master RTX 5090 D-RGB Water Block - Nickel + Plexi", "rtx-5090", False, "accessory"),
    ("Bykski GPU Water Block for ASUS ROG Astral RTX 5080", "rtx-5080", False, "accessory"),
    ("Alphacool Core GeForce RTX 5090 Reference with Backplate", "rtx-5090", False, "accessory"),
    ("LINKUP PCIe 5.0 Riser Cable for RTX 5090 Vertical Mount", "rtx-5090", False, "accessory"),
    ("NVIDIA GeForce RTX NVLink Bridge 3-Slot for RTX 3090", "rtx-5090", False, "accessory"),
    ("CableMod 12VHPWR Cable for RTX 5090 Black", "rtx-5090", False, "accessory"),
    ("upHere GPU Support Bracket for RTX 5090 Anti-Sag Graphics Card Holder", "rtx-5090", False, "accessory"),
    ("(No GPU) ROG Astral GeForce RTX 5090 Graphics Card, White, Only as Decoration or Display", "rtx-5090", False, "accessory"),
    ("Gigabyte AORUS RTX 5090 AI BOX GV-N5090IXEB-32GD GDDR7 28Gbps Thunderbolt", "rtx-5090", False, "accessory"),
    ("ASUS ROG XG Mobile GeForce RTX 5070 Ti Thunderbolt 5", "rtx-5070-ti", False, "accessory"),
    ("Khadas Mind Graphics 2 eGPU External Graphics Card with NVIDIA RTX 5060 Ti 16GB GDDR7", "rtx-5060-ti-16gb", False, "accessory"),
    ("100MM 6PIN DC 12V GPU Fan for Intel ARC B580 Graphics Card Cooling", "arc-b580", False, "accessory"),
    ("Cooler Master HAF 700 EVO E-ATX High Airflow Case, Compatible with RX 9070 XT | RTX 5090", "rtx-5090", False, "accessory"),
    ("EKWB EK-Quantum Vector3 FE RTX 5090 D-RGB ABP Set - Nickel + Plexi", "rtx-5090", False, "accessory"),
    ("Thermal Grizzly WireView Pro for RTX 5090 12V-2x6", "rtx-5090", False, "accessory"),
    ("MSI Gaming GeForce RTX 5070 Ti 16G Gaming Trio OC Plus with Anti-Sag Bracket", "rtx-5070-ti", True, "match"),
    ("Sapphire Nitro+ AMD Radeon RX 9070 XT Gaming OC 16GB GDDR6 with free game bundle", "rx-9070-xt", True, "match"),
    ("ASUS Prime Radeon RX 9070 XT OC GPU + Prime 850W PSU", "rx-9070-xt", False, "bundle"),
    ("MSI GeForce RTX 5060 Ti 8G VENTUS 2X OC PLUS Graphic Card, PRO B650-S WIFI Motherboard and MAG A750GL PCIE5 750W, Fully Modular Power Supply Bundle",
     "rtx-5060-ti-8gb", False, "bundle"),
    # Not a GPU at all.
    ("LEGO Star Wars ARC-170 Starfighter Building Toy for Kids 75402", "arc-b580", False, "accessory"),
    ("IK MULTIMEDIA ARC On-Ear USB-C Audio Interface, Black", "arc-b580", False, "other"),
]


@pytest.mark.parametrize("title,model_id,ok,reason", MATCH_CASES)
def test_match_truth_table(title, model_id, ok, reason):
    result = match(title, m(model_id))
    assert isinstance(result, MatchResult)
    assert result.ok is ok, (title, result)
    assert result.reason.startswith(reason), (title, result)


# (title, kind, models, vram)
PARSE_CASES = [
    ("PNY ARGB GeForce  RTX5070 Ti ARGB EPIC-X RGB Triple Fan, 16GB 256-Bit GDDR7 RTX 5070 T i Graphics Card VCG5070T16TFXXPB1-O",
     Kind.GPU, ("RTX 5070 TI",), 16),
    ("SAPPHIRE NITRO+RX 9070 XT 16GB GDDR6", Kind.GPU, ("RX 9070 XT",), 16),
    ("SAPPHIRE PULSE Radeon RX 9070 XT Graphics Card 11348-03-20G", Kind.GPU, ("RX 9070 XT",), None),
    ("ASUS The SFF-Ready Prime GeForce RTX� 5070 12GB GDDR7 White OC Edition Graphics Card", Kind.GPU, ("RTX 5070",), 12),
    ("ASUS Dual GeForce RTX™ 5060 8GB GDDR7 OC Edition", Kind.GPU, ("RTX 5060",), 8),
    ("Lenovo Legion Pro 5i 16\" OLED Ultra 9-275HX RTX 5070 Ti 32GB 1TB WIN11", Kind.LAPTOP, ("RTX 5070 TI",), None),
    ("AMD Radeon RX 7600M XT 8GB", Kind.LAPTOP, ("RX 7600",), 8),
    ("ARMOURY AMD Ryzen 7 8700F Radeon RX 9070 32GB RAM 1TB SSD W11H", Kind.PREBUILT, ("RX 9070",), None),
    ("ASUS NUC 15 Pro Slim Intel Ultra 7 255H 32GB RAM 1TB SSD Win11 Pro", Kind.PREBUILT, (), None),
    ("Intel Core Ultra 7 265KF - Core Ultra 7 (Series 2) Arrow Lake 20-Core, LGA 1851, 125W Desktop Processor",
     Kind.OTHER, (), None),
    ("Graphics Card GPU Brace Support, Video Card Sag Holder Bracket, GPU Stand with Magnet", Kind.ACCESSORY, (), None),
    ("Cooler Master HAF 700 EVO E-ATX Case, Compatible with RX 9070 XT | RTX 5090", Kind.ACCESSORY, ("RX 9070 XT", "RTX 5090"), None),
    ("EK-Quantum Vector2 Master RTX 5090 D-RGB Water Block", Kind.ACCESSORY, ("RTX 5090",), None),
    ("MSI MAG A750GL PCIE5 750W Power Supply", Kind.OTHER, (), None),
]


@pytest.mark.parametrize("title,kind,models,vram", PARSE_CASES)
def test_parse_title(title, kind, models, vram):
    p = parse_title(title)
    assert isinstance(p, ParsedTitle)
    assert (p.kind, p.models, p.vram_gb) == (kind, models, vram), title


@pytest.mark.parametrize("raw,expected", [
    ("RTX5070TI", "RTX 5070 TI"),
    ("9070XT", "9070 XT"),
    ("TUF-RTX5060TI-O16G-GAMING", "TUF RTX 5060 TI O 16 G GAMING"),
    ("GeForce RTX™ 5070", "GEFORCE RTX 5070"),
    ("Radeon® RX 9070 GRE", "RADEON RX 9070 GRE"),
    # Accented letters lose the accent; symbols still become spaces.
    ("Câble d'alimentation, carte mère", "CABLE D ALIMENTATION CARTE MERE"),
    ("RTX™ 5080 × 2", "RTX 5080 2"),
])
def test_normalise(raw, expected):
    assert normalise(raw) == expected


@pytest.mark.parametrize("title,kind", [
    # French accessory, bundle, condition and computer wording.
    ("Bloc d'eau pour carte graphique RTX 5090", Kind.ACCESSORY),
    ("Câble d'alimentation 12V-2x6 pour RTX 5090", Kind.ACCESSORY),
    ("Câble d'alimentation 12VHPWR RTX 5090 GPU", Kind.ACCESSORY),
    ("Support de carte graphique pour RTX 5090", Kind.ACCESSORY),
    ("Ventilateur pour RTX 5080 16GB", Kind.ACCESSORY),
    ("Carte graphique RTX 5080 16 Go avec bloc d'alimentation 850 W", Kind.BUNDLE),
    ("Ensemble carte graphique RTX 5070 et carte mère B850", Kind.BUNDLE),
    ("Carte graphique ASUS Prime GeForce RTX 5080 16 Go GDDR7 OC - Défectueuse, pour pièces seulement", Kind.OTHER),
    ("Boîte vide pour RTX 5080 16GB", Kind.OTHER),
    ("MSI GeForce RTX 5080 16G - Ne fonctionne pas, vendue telle quelle", Kind.OTHER),
    ("Ordinateur de bureau RTX 5080 16GB", Kind.PREBUILT),
    ("PC de jeu GeForce RTX 5080 16 Go, 32 Go DDR5, SSD 2 To", Kind.PREBUILT),
    ("Ordinateur portable ASUS ROG RTX 5080 16GB", Kind.LAPTOP),
    ("PC Portable Gamer RTX 5080 16GB", Kind.LAPTOP),
])
def test_french_wording(title, kind):
    assert parse_title(title).kind is kind


@pytest.mark.parametrize("title,model_id", [
    ("Carte graphique GeForce RTX 5080 16 Go GDDR7 de MSI", "rtx-5080"),
    ("Sapphire PULSE AMD RADEON RX 9070 GRE GAMING OC Carte Graphique 12GB", "rx-9070-gre"),   # real Amazon.ca row
    ("ASUS TUF Gaming GeForce RTX™ 5080 16Go GDDR7 Édition OC", "rtx-5080"),
])
def test_french_card_titles_match(title, model_id):
    assert match(title, m(model_id)).ok


def test_whole_tokens_only():
    # Substrings that fooled v1 must never become suffix tokens.
    for title in ("RTX 5070 MULTI", "RTX 5070 EDITION", "RX 7600 NEXT", "RX 7600 FSR EXTRA"):
        assert parse_title(title).models in (("RTX 5070",), ("RX 7600",)), title


@pytest.mark.parametrize("title,model_id", [
    ("GeForce RTX 5070TIOC 16G", "rtx-5070"),
    ("GeForce RTX 5070TIOC 16G", "rtx-5070-ti"),
    ("Radeon RX 9070XTOC 16G", "rx-9070"),
    ("Radeon RX 9070XTOC 16G", "rx-9070-xt"),
])
def test_suffix_glued_to_other_letters_matches_nothing(title, model_id):
    # Precision first: an unreadable suffix is neither the base model nor the suffixed one.
    assert not match(title, m(model_id)).ok


def test_card_sold_with_holder_is_still_a_card():
    title = "CyberGeek GeForce RTX 5070 Ti Overclocked Triple Fan Graphics Card, 16GB GDDR7, DLSS 4, with GPU Holder"
    assert match(title, m("rtx-5070-ti")).ok


@pytest.mark.parametrize("title", [
    # A card plus a PSU, motherboard or case, however long or hyphenated the second product's name.
    "ASUS Prime Radeon RX 9070 XT OC GPU + ASUS Prime 850W Gold PSU",
    "ASUS TUF Gaming GeForce RTX 5070 Ti OC GPU + TUF Gaming 850W Gold PSU",
    "ASUS ROG Strix GeForce RTX 5080 OC GPU + ROG Strix 1000W Platinum PSU",
    "MSI GeForce RTX 5070 12G Ventus 2X OC + MSI MAG A850GL PCIE5 850W PSU",
    "ASUS Prime GeForce RTX 5070 OC GPU + Prime 750W 80+ Gold PSU",
    "ASUS Prime GeForce RTX 5070 OC GPU + ASUS Prime 750W Gold Power Supply",
    "ASUS Prime GeForce RTX 5070 Ti OC GPU + Prime B850-PLUS WIFI Motherboard",
    "MSI GeForce RTX 5070 Ti 16GB Graphics Card + PRO-B650-S Motherboard",
    "ASUS Prime RTX 5080 16GB GPU + ROG-STRIX-850G PSU",
    "ASUS Prime GeForce RTX 5070 OC GPU + Prime AP202 mATX Case",
    "MSI RTX 5080 16G Ventus + MSI MAG A850GL 850W",
])
def test_card_plus_psu_motherboard_or_case_is_a_bundle(title):
    assert parse_title(title).kind is Kind.BUNDLE


@pytest.mark.parametrize("title,model_id", [
    # A game promotion is the card at the card's price, whatever the promotion is called.
    ("MSI GeForce RTX 5070 Ti 16G Ventus 3X OC (Battlefield 6 Bundle)", "rtx-5070-ti"),
    ("ASUS TUF Gaming GeForce RTX 5080 16GB GDDR7 OC, DOOM: The Dark Ages Bundle", "rtx-5080"),
    ("PowerColor Reaper RX 9070 XT 16GB - AMD Gaming Bundle", "rx-9070-xt"),
    ("Sapphire Pulse Radeon RX 9070 XT 16GB GDDR6, Free Games Bundle", "rx-9070-xt"),
    ("GIGABYTE GeForce RTX 5070 Ti Gaming OC 16GB with Bundle", "rtx-5070-ti"),
    ("ASUS Prime Radeon RX 9070 XT OC 16GB - AMD Gaming Bundle, 750W PSU Recommended", "rx-9070-xt"),
])
def test_game_promotion_is_not_a_bundle(title, model_id):
    assert match(title, m(model_id)).ok


@pytest.mark.parametrize("title", [
    "MSI GeForce RTX 5060 Ti 8G VENTUS 2X OC PLUS Graphic Card, PRO B650-S WIFI Motherboard and MAG A750GL PCIE5 750W, "
    "Fully Modular Power Supply Bundle",
    "MSI RTX 5080 16G Ventus + Corsair RM1000x 1000W Bundle",
    'RTX 5080 16GB Graphics Card Combo with 27" 4K display',
])
def test_hardware_bundle_and_combo_are_bundles(title):
    assert parse_title(title).kind is Kind.BUNDLE


@pytest.mark.parametrize("title,model_id", [
    # Feature text on a real card: an included or bundled accessory, cooler and platform wording.
    ("MSI GeForce RTX 5070 Ti 16G Gaming Trio OC Graphics Card, Support Bracket Included", "rtx-5070-ti"),
    ("MSI GeForce RTX 5070 Ti 16G Gaming Trio OC Graphics Card, GPU Holder included", "rtx-5070-ti"),
    ("MSI GeForce RTX 5070 Ti 16G Gaming Trio OC Graphics Card, Bundled Anti-Sag Bracket", "rtx-5070-ti"),
    ("MSI GeForce RTX 5070 Ti 16G Gaming Trio OC Graphics Card, Triple fans for better cooling", "rtx-5070-ti"),
    ("MSI GeForce RTX 5070 Ti 16G Gaming Trio OC Graphics Card, Compatible with Intel and AMD Platforms", "rtx-5070-ti"),
    ("MSI Gaming GeForce RTX 4070 12GB GDRR6X 192-Bit HDMI/DP Nvlink TORX Fan 4.0 Ada Lovelace Architecture "
     "Graphics Card (RTX 4070 Ventus 2X 12G OC)", "rtx-4070"),
    # The card's own M.2 slot and a CPU brand in feature text are not a PC's specs.
    ("ASUS Dual GeForce RTX 4060 Ti SSD OC Edition Gaming Graphics Card (PCIe 4.0, 8GB GDDR6, DLSS 3, "
     "M.2 NVMe SSD Slot)", "rtx-4060-ti-8gb"),
    ("Sapphire Pulse AMD Radeon RX 9070 XT 16GB GDDR6 Graphics Card, AMD Smart Access Memory with Ryzen", "rx-9070-xt"),
])
def test_card_feature_text_is_not_an_accessory_or_pc(title, model_id):
    assert match(title, m(model_id)).ok


@pytest.mark.parametrize("title", [
    # A card sold in a PC with its CPU named is still a PC.
    "Gaming PC Graphics Card RTX 5070 12GB GDDR7 with AMD Ryzen 7 9800X3D",
    "NVIDIA GeForce RTX 5080 GPU, Intel Core Ultra 9 285K, Gaming Desktop",
])
def test_card_noun_with_a_numbered_cpu_is_a_pc(title):
    assert parse_title(title).kind is Kind.PREBUILT


@pytest.mark.parametrize("title", [
    # A card noun right before the accessory word names what the accessory fits (first is a real Amazon row).
    "Alphacool Core Geforce RTX 5080 Solid + AMP GPU Water Block with Backplate",
    "Alphacool Core RTX 5090 Reference GPU Cooler with Backplate",
    # The made-for rule reads hyphenated names.
    "Thermal Grizzly WireView Pro for ROG-STRIX RTX 5090 GPU",
])
def test_accessory_named_with_a_card_noun_is_an_accessory(title):
    assert parse_title(title).kind is Kind.ACCESSORY


def test_multiple_models_never_match():
    result = match("ASUS Dual GeForce RTX 5060 Ti 16GB and RTX 4060 Ti 8GB Graphics Card", m("rtx-5060-ti-16gb"))
    assert not result.ok and result.reason.startswith("multiple models")


def test_price_is_not_part_of_matching():
    # Floors live in the pricing layer; match() only looks at the title.
    assert match("MSI GeForce RTX 5070 12G VENTUS 2X OC", m("rtx-5070")).ok


def test_every_catalog_model_matches_its_own_name():
    for model in catalog.load():
        vram = f" {model.vram_gb}GB" if model.vram_gb else ""
        title = f"ASUS Dual GeForce {model.name}{vram} GDDR7 Graphics Card"
        if model.vendor == "amd":
            title = f"Sapphire Pulse AMD Radeon {model.name}{vram} GDDR6 Gaming GPU"
        if model.vendor == "intel":
            title = f"Sparkle Intel {model.name} Titan OC {model.vram_options[0]}GB GDDR6 Graphics Card"
        assert match(title, model).ok, (title, match(title, model))


@pytest.mark.parametrize("title,shadowed_ok", [
    # A later, more specific mention is another model named in passing, not this card.
    ("MSI RTX 5070 12G Ventus (RTX 5070 Ti cooler design)", "rtx-5070-ti"),
    ("MSI RTX 5070 12G Ventus (RTX 5070 Ti cooler design)", "rtx-5070"),
    ("AMD Radeon RX 9070 Graphics Card - beats RX 9070 XT", "rx-9070-xt"),
    ("AMD Radeon RX 9070 Graphics Card - beats RX 9070 XT", "rx-9070"),
])
def test_later_specific_mention_does_not_replace_the_model(title, shadowed_ok):
    p = parse_title(title)
    assert len(p.models) == 2, p
    assert not match(title, m(shadowed_ok)).ok


@pytest.mark.parametrize("title,model_id", [
    # Two models named as alternatives: whichever comes first, the title is not one card.
    ("MSI GeForce RTX 5070 Ti / RTX 5070 Graphics Card", "rtx-5070-ti"),
    ("RTX 5070 Ti and RTX 5070 Graphics Card", "rtx-5070-ti"),
    ("GeForce RTX 5070 Ti vs RTX 5070 16GB", "rtx-5070-ti"),
    ("RTX 5070 Ti or RTX 5070 Graphics Card", "rtx-5070-ti"),
    ("RX 9070 XT and RX 9070 Graphics Card", "rx-9070-xt"),
    # A bare number listed after a mention is the same family's model.
    ("GeForce RTX 5070 Ti/5070 Graphics Card 16GB/12GB", "rtx-5070-ti"),
    ("MSI RTX 5070 / 5070 Ti", "rtx-5070"),
    ("AMD Radeon RX 9070 XT/9070 GDDR6 16GB", "rx-9070-xt"),
    ("RTX 5060 Ti/5060 16GB GDDR7", "rtx-5060-ti-16gb"),
    ("Graphics Card RTX 5070/5070 Ti/5080 GDDR7", "rtx-5070"),
    ("PowerColor RX 9070 XT | 9070 16GB", "rx-9070-xt"),
    ("RTX 5070 Ti & 5070 Graphics Card", "rtx-5070-ti"),
    ("RTX 5070 Ti or 5070 Graphics Card", "rtx-5070-ti"),
    # A listed alternative in parentheses or before SERIES is still an alternative, not a restatement.
    ("Graphics Card (RTX 5070 Ti / RTX 5070)", "rtx-5070-ti"),
    ("MSI GeForce RTX 5070 Ti Graphics Card (RTX 5070 Ti/5070)", "rtx-5070-ti"),
    ("RTX 5070 Ti / RTX 5070 Series Graphics Card", "rtx-5070-ti"),
    ("RTX 5070 Ti / NVIDIA GeForce RTX 5070 Series Graphics Card", "rtx-5070-ti"),
    ("Radeon RX 9070 XT Graphics Card (AMD Radeon RX 9070 XT or RX 9070)", "rx-9070-xt"),
])
def test_listed_alternatives_are_multiple_models(title, model_id):
    result = match(title, m(model_id))
    assert not result.ok and result.reason.startswith("multiple models"), result


@pytest.mark.parametrize("title,model_id", [
    # A number after a separator that is not a model: clock, size, bus width, resolution.
    ("MSI GeForce RTX 5070 / 2512 MHz Boost Clock", "rtx-5070"),
    ("AMD Radeon RX 9070 XT / 2048 MB Cache", "rx-9070-xt"),
    ("GeForce RTX 5070/1440p Gaming Graphics Card", "rtx-5070"),
    # A number outside that family's model numbering: resolution or year.
    ("AMD Radeon RX 9070 XT/1440p Gaming Graphics Card", "rx-9070-xt"),
    ("AMD Radeon RX 9070 XT / 2160p 4K Gaming Graphics Card", "rx-9070-xt"),
    ("PowerColor RX 9070 XT / 7680x4320", "rx-9070-xt"),
    ("NVIDIA GeForce RTX 5090 / 2025 Edition Graphics Card", "rtx-5090"),
    # A comparison names another card in passing (real eval title).
    ("Graphics Card Fit for GUNNIR, Arc B580 Index 12GB GDDR6 Comparable to 4060Ti Desktop Gaming Discrete Graphics Card",
     "arc-b580"),
])
def test_number_after_separator_that_is_not_a_model(title, model_id):
    assert match(title, m(model_id)).ok


@pytest.mark.parametrize("title,models", [
    # The specific key first, or from a part code, still shadows the base mention when the base
    # is a restatement in parentheses or before SERIES, or a part code.
    ("ASUS Prime Radeon RX 9070 GRE OC Edition Gaming Graphics Card (AMD Radeon RX 9070, PCIe 5.0)", ("RX 9070 GRE",)),
    ("ASUS ROG Astral BTF GeForce RTX 5090 24GB GDDR7 BTF OC Edition ROG-ASTRAL-RTX5090DV2-O24G-BTF", ("RTX 5090 D V2",)),
    ("GIGABYTE Radeon RX 9070 Graphics Card GV-R907XGAMING OC-16GD", ("RX 9070 XT",)),
    ("XFX Swift AMD Radeon RX 9070 GRE Triple Fan Gaming Edition with 12GB GDDR6 HDMI 3xDP, AMD RDNA 4 RX-97GRE29BA",
     ("RX 9070 GRE",)),
    ("Gigabyte Radeon RX 9070 GRE Gaming OC 12G, Radeon RX 9070 Series", ("RX 9070 GRE",)),
])
def test_specific_key_first_or_from_part_code_shadows_base(title, models):
    assert parse_title(title).models == models


@pytest.mark.parametrize("title,key", [
    ("ASUS TUF Gaming GeForce RTX 4080S OC 16GB GDDR6X Graphics Card", "RTX 4080 SUPER"),
    ("MSI GeForce RTX 4080 S 16GB Gaming X Slim Graphics Card", "RTX 4080 SUPER"),
    ("Zotac RTX 4070S Twin Edge 12GB GDDR6X Graphics Card", "RTX 4070 SUPER"),
    ("Gigabyte RTX 4070 Ti S 16GB Eagle OC Graphics Card", "RTX 4070 TI SUPER"),
    ("RTX 4070 Ti-S 16GB GDDR6X Graphics Card", "RTX 4070 TI SUPER"),
    ("GeForce RTX 5070 T.I. 16GB Graphics Card", "RTX 5070 TI"),
])
def test_nvidia_s_shorthand_is_super(title, key):
    assert parse_title(title).models == (key,)


@pytest.mark.parametrize("title,model_id", [
    ("ASUS TUF Gaming GeForce RTX 4080S OC 16GB GDDR6X Graphics Card", "rtx-4080-super"),
    ("Zotac RTX 4070S Twin Edge 12GB GDDR6X Graphics Card", "rtx-4070-super"),
    ("Gigabyte RTX 4070 Ti S 16GB Eagle OC Graphics Card", "rtx-4070-ti-super"),
])
def test_nvidia_s_shorthand_matches_super_model(title, model_id):
    assert match(title, m(model_id)).ok


@pytest.mark.parametrize("title,model_id,super_id", [
    # A possessive 's is not the S shorthand for SUPER.
    ("MSI RTX 4070 Ti's Gaming X Trio 12G", "rtx-4070-ti", "rtx-4070-ti-super"),
    ("Upgrade to the RTX 4080's 16GB GDDR6X Graphics Card", "rtx-4080", "rtx-4080-super"),
    ("NVIDIA GeForce RTX 5090's 32GB GDDR7 Founders Edition", "rtx-5090", None),
    ("NVIDIA GeForce RTX 5090’s 32GB GDDR7 Founders Edition", "rtx-5090", None),
])
def test_possessive_is_not_super(title, model_id, super_id):
    assert match(title, m(model_id)).ok
    if super_id:
        assert not match(title, m(super_id)).ok


@pytest.mark.parametrize("title,base_id", [
    ("ASUS TUF Gaming GeForce RTX 4080S OC 16GB GDDR6X Graphics Card", "rtx-4080"),
    ("Zotac RTX 4070S Twin Edge 12GB GDDR6X Graphics Card", "rtx-4070"),
    ("RTX 4070 Ti-S 16GB GDDR6X Graphics Card", "rtx-4070-ti"),
    # Any other stray letter is an unknown suffix: neither the base model nor a guess.
    ("ASUS RTX5070T 16GB Graphics Card", "rtx-5070"),
    ("ASUS RTX5070T 16GB Graphics Card", "rtx-5070-ti"),
    ("GeForce RTX 5090DD 32GB Graphics Card", "rtx-5090"),
    ("GeForce RTX 5080 D 16GB Graphics Card", "rtx-5080"),
    ("ASRock Radeon RX 9070 X T Steel Legend 16GB Graphics Card", "rx-9070"),
    ("ASRock Radeon RX 9070 X T Steel Legend 16GB Graphics Card", "rx-9070-xt"),
    ("Radeon RX 7900XT X 24GB", "rx-7900-xt"),
    ("GeForce RTX 5070 V 12GB Graphics Card", "rtx-5070"),
])
def test_unexpected_letter_suffix_matches_nothing(title, base_id):
    assert not match(title, m(base_id)).ok


@pytest.mark.parametrize("title,model_id", [
    # A revision (V2), a glued OC or FE, and a glued VRAM size are not suffixes.
    ("ASUS Dual GeForce RTX 4060 V2 OC Edition 8GB GDDR6 DUAL-RTX4060-O8G-V2", "rtx-4060"),
    ("ASUS Dual NVIDIA GeForce RTX 4060 V2 8GB GDDR6 Video Card", "rtx-4060"),
    ("ASUS Dual GeForce RTX 5060 V2 8GB", "rtx-5060"),
    ("NVIDIA GeForce RTX5080FE 16GB", "rtx-5080"),
    ("Gigabyte GeForce RTX5070OC 12GB", "rtx-5070"),
    ("ASUS GeForce RTX 508016GB GDDR7 Graphics Card", "rtx-5080"),
    ("ASUS GeForce RTX508016GB GDDR7 Graphics Card", "rtx-5080"),
    ("ASUS PRIME GeForce RTX 507012GB 192-Bit GDDR7 PCI Express 5.0 Graphics Card PRIME-RTX5070-O12G", "rtx-5070"),
])
def test_revision_glued_oc_and_glued_vram_are_not_suffixes(title, model_id):
    assert match(title, m(model_id)).ok


def test_mobile_marker_after_glued_vram():
    assert parse_title("NVIDIA GeForce RTX 507012GB Mobile").kind is Kind.LAPTOP


@pytest.mark.parametrize("title,model_id", [
    # A letter that opens a VRAM code is not a suffix.
    ("ASUS Matrix GeForce RTX 5090 Graphics Card ROG-MATRIX-RTX5090-P32G-30TH", "rtx-5090"),
    ("PowerColor Reaper Radeon RX 9070 XT Graphics Card RX9070XT Z16G-A", "rx-9070-xt"),
    ("EK-Quantum Vector2 Master RTX 5090 D-RGB Water Block", None),
])
def test_vram_code_letters_are_not_suffixes(title, model_id):
    if model_id is None:
        assert parse_title(title).models == ("RTX 5090",)
    else:
        assert match(title, m(model_id)).ok


@pytest.mark.parametrize("title,model_id", [
    ("MSI GeForce RTX 5060 Ti 8GB/16GB Gaming OC Graphics Card", "rtx-5060-ti-16gb"),
    ("MSI GeForce RTX 5060 Ti 8GB/16GB Gaming OC Graphics Card", "rtx-5060-ti-8gb"),
    ("PowerColor Reaper RX 9060 XT 8GB/16GB", "rx-9060-xt-16gb"),
    ("GIGABYTE RTX 5060 Ti 16GB vs 8GB", "rtx-5060-ti-16gb"),
    ("GIGABYTE RTX 5060 Ti 16GB vs 8GB", "rtx-5060-ti-8gb"),
    # Two sizes sharing one unit.
    ("MSI GeForce RTX 5060 Ti 8/16GB GDDR7 Graphics Card", "rtx-5060-ti-16gb"),
    ("MSI GeForce RTX 5060 Ti 8/16GB GDDR7 Graphics Card", "rtx-5060-ti-8gb"),
    ("MSI GeForce RTX 5060 Ti 8 or 16GB GDDR7", "rtx-5060-ti-16gb"),
    ("PowerColor Reaper RX 9060 XT 8|16GB", "rx-9060-xt-16gb"),
    ("PowerColor Reaper RX 9060 XT 16/8 GB", "rx-9060-xt-8gb"),
])
def test_two_vram_variants_in_one_title_are_vram_unknown(title, model_id):
    result = match(title, m(model_id))
    assert not result.ok and result.reason == "vram unknown", result


@pytest.mark.parametrize("title,model_id,reason", [
    # A size the model is never sold with is a sibling whose suffix the title left out.
    ("AMD Radeon RX 9070 OC 12GB GDDR6 Video Card", "rx-9070", "vram 12 != 16"),
    ("ASUS ROG Astral BTF GeForce RTX 5090 24GB GDDR7 BTF OC Edition Graphics Card", "rtx-5090", "vram 24 != 32"),
    ("MSI GeForce RTX 4070 Ti 16G Ventus 3X OC", "rtx-4070-ti", "vram 16 != 12"),
    ("Sparkle Intel Arc B570 Guardian OC 12GB GDDR6", "arc-b570", "vram 12 != 10"),
])
def test_vram_the_model_never_has_rejects(title, model_id, reason):
    result = match(title, m(model_id))
    assert not result.ok and result.reason == reason, result


@pytest.mark.parametrize("title,model_id", [
    ("AMD Radeon RX 9070 16GB GDDR6 Video Card", "rx-9070"),
    ("ASUS Prime Radeon RX 9070 OC GDDR6 Triple Fans", "rx-9070"),      # no VRAM stated: still a match
    ("Sparkle Intel Arc B570 Guardian OC 10GB GDDR6", "arc-b570"),
])
def test_vram_the_model_has_or_no_vram_matches(title, model_id):
    assert match(title, m(model_id)).ok


@pytest.mark.parametrize("title,vram", [
    # System RAM then storage is still skipped.
    ("Skytech Gaming PC Desktop, Intel i5 14400F, RTX 5060 Ti 8GB, 16GB, 1TB", 8),
    ("MSI Crosshair 18 HX AI 18\" Ultra 9 275HX RTX 5070 12GB 32GB 1TB Win11H", 12),
    ("GeForce RTX 5060 Ti 8GB 16GB 512GB SSD", 8),
])
def test_ram_then_storage_is_not_vram(title, vram):
    assert parse_title(title).vram_gb == vram


@pytest.mark.parametrize("title,model_id", [
    # Marketplace spellings of VRAM on a card.
    ("GeForce RTX 5060 Ti with 16GB memory", "rtx-5060-ti-16gb"),
    ("GeForce RTX 5060 Ti 16GB Memory GDDR7", "rtx-5060-ti-16gb"),
    ("GeForce RTX 5060 Ti Graphics Card, 16GB Memory GDDR7", "rtx-5060-ti-16gb"),
    ("Radeon RX 9060 XT 16GB Memory Graphics Card", "rx-9060-xt-16gb"),
    ("GeForce RTX 5060 Ti 16GB DDR7", "rtx-5060-ti-16gb"),
    ("GeForce RTX 5060 Ti 16GBGDDR7", "rtx-5060-ti-16gb"),
    ("Radeon RX 9060 XT 8GB DDR6 Graphics Card", "rx-9060-xt-8gb"),
])
def test_vram_spellings(title, model_id):
    assert match(title, m(model_id)).ok


@pytest.mark.parametrize("title,vram", [
    # System memory is still not VRAM: DDR4/DDR5, and "memory" in a PC title.
    ("GeForce RTX 5060 Ti Gaming Graphics Card, 16GB DDR5 System Recommended, 8GB GDDR7", 8),
    ("CyberPowerPC Gaming PC, AMD Ryzen 7 8700F, GeForce RTX 5060 Ti 8GB GDDR7, 32GB Memory, 1TB SSD", 8),
    ("Gaming Desktop PC, Intel Core i7-14700F, GeForce RTX 5060 Ti, 16GB Memory, 1TB SSD", None),
    ("MSI Sword 16 HX Gaming Laptop, RTX 5060, 16GB Memory, 1TB SSD", None),
    # Prebuilts the kind rules read as a card (a card noun and no CPU or specs): memory that does not
    # follow the model is still system RAM.
    ("iBUYPOWER Y40 Gaming PC Desktop, NVIDIA GeForce RTX 5060 Ti GPU, 16GB Memory", None),
    ("Lenovo Legion Tower 5 with RTX 5060 Ti GPU, 16GB Memory", None),
    ("CyberPowerPC Gamer Xtreme, GeForce RTX 5060 Ti GPU, 16GB Memory", None),
    ("Gaming PC GeForce RTX 5060 Ti GPU 16GB Memory", None),
    ("Gaming Desktop RTX 5060 Ti Graphics Card 16GB Memory", None),
])
def test_system_memory_is_not_vram(title, vram):
    assert parse_title(title).vram_gb == vram


@pytest.mark.parametrize("title,kind", [
    ("NVIDIA GeForce RTX 5090 Founders Edition 32GB GDDR7 (For Parts, Not Working)", Kind.OTHER),
    ("ASUS ROG Astral RTX 5090 32GB - Broken, Defective, As-Is", Kind.OTHER),
    ("ZOTAC GAMING GeForce RTX 5070 Ti SOLID 16GB GDDR7 - Sold As Is, No Display", Kind.OTHER),
    ("ASUS TUF Gaming GeForce RTX 5090 32GB (AS IS)", Kind.OTHER),
    ("MSI GeForce RTX 5080 16G Gaming Trio OC - No Display, Untested", Kind.OTHER),
    ("GIGABYTE GeForce RTX 5090 Gaming OC 32GB - Damaged Connector", Kind.OTHER),
    ("ASUS ROG Astral RTX 5090 32GB - Melted 12V-2x6 Connector, Faulty", Kind.OTHER),
    ("MSI GeForce RTX 5080 16G Ventus 3X OC - Artifacting, Read Description", Kind.OTHER),
    ("ZOTAC GAMING GeForce RTX 5070 Ti SOLID 16GB - Cracked PCB", Kind.OTHER),
    ("PNY GeForce RTX 5080 16GB OC - Not Tested, No Returns", Kind.OTHER),
    ("NVIDIA GeForce RTX 5090 32GB Graphics Card Non-Working", Kind.OTHER),
    ("NVIDIA GeForce RTX 5090 Founders Edition 32GB Parts Only", Kind.OTHER),
    ("MSI GeForce RTX 5090 32G Suprim Liquid SOC - Burnt Power Connector", Kind.OTHER),
    ("NVIDIA GeForce RTX 5090 Founders Edition - Empty Box Only", Kind.OTHER),
    ("MSI RTX 5080 Gaming Trio OC 16G box and manual", Kind.OTHER),
    ("RTX 5090 32GB GDDR7 Graphics Card Repair Service", Kind.OTHER),
    ("NVIDIA RTX 5090 32GB Graphics Card Rental (per month)", Kind.OTHER),
    ("ASUS ROG Astral RTX 5090 32GB GDDR7 Gaming Graphics Card Gift Card", Kind.OTHER),
    ("Lot of 2: RTX 5070 Ti and RTX 5070", Kind.OTHER),
    ("2-Pack NVIDIA RTX 5090 32GB Graphics Card", Kind.OTHER),
    ("Bykski RTX 5090 GPU Liquid Cooling Block", Kind.ACCESSORY),
    ("EKWB EK-Quantum Vector2 RTX 5090 Water-Block", Kind.ACCESSORY),
    ("Corsair 12V-2x6 PCIe 5.1 GPU Power Cord RTX 5090", Kind.ACCESSORY),
    ("RTX 5090 Graphics Card Support Pillar", Kind.ACCESSORY),
    ("GPU Backplate RTX 5090 32GB", Kind.ACCESSORY),
    ("Acer Nitro 16 Gaming RTX 5060 8GB", Kind.LAPTOP),
    ("ASUS TUF Gaming A16 RTX 5060 8GB", Kind.LAPTOP),
    ("ASUS ROG Flow Z13 RTX 5070 8GB", Kind.LAPTOP),
    ("Razer Blade 16 RTX 5070 Ti 12GB", Kind.LAPTOP),
    # The word Laptop, a mobile marker after the VRAM, or a laptop name outweighs GPU or GDDR.
    ("NVIDIA GeForce RTX 5070 Ti 12GB Laptop GPU", Kind.LAPTOP),
    ("RTX 5070 8GB Mobile GPU", Kind.LAPTOP),
    ("NVIDIA GeForce RTX 5090 24GB GDDR7 Laptop GPU", Kind.LAPTOP),
    ("RTX 5080 16GB GDDR7 GPU Gaming Laptop", Kind.LAPTOP),
    ("Gaming Laptop with NVIDIA GeForce RTX 5080 16GB GDDR7 GPU", Kind.LAPTOP),
    ("ASUS ROG Strix G16 Gaming Laptop, NVIDIA GeForce RTX 5070 Ti 12GB GDDR7", Kind.LAPTOP),
    ("MSI Sword 16 HX RTX 5070 8GB GDDR7", Kind.LAPTOP),
    ("Lenovo LOQ 15 RTX 5060 8GB GDDR7", Kind.LAPTOP),
    # CPU spellings and DDR5 system memory next to a card noun.
    ("Intel Core 7 240H, NVIDIA GeForce RTX 5070 8GB GDDR7, 16GB DDR5", Kind.LAPTOP),
    ("CyberPowerPC Gamer Xtreme, Intel i7 14700F, GeForce RTX 5070 Ti 16GB GDDR7", Kind.PREBUILT),
    ("ASUS ROG G700, GeForce RTX 5070 12GB GDDR7, 32GB DDR5, 512GB SSD", Kind.PREBUILT),
])
def test_non_card_listings_are_not_gpus(title, kind):
    assert parse_title(title).kind is kind


@pytest.mark.parametrize("title,model_id", [
    # Real eval cards that name a computer: the card noun keeps them cards.
    ("XFX Mercury AMD Radeon RX 9070XT OC Magnetic Air Edition with RGB 16GB GDDR6 HDMI 3xDP, RDNA 4 RX-97TMARGBA, "
     "Graphics Card, Desktop PC", "rx-9070-xt"),
    ("Yeston Sakura Radeon RX 9070 XT 16G GDDR6 OC SA 4nm Desktop Computer PC Video Graphics Cards Support "
     "PCI-Express 5.0 3*DP+1*HDMI-Compatible ARGB Graphics Card", "rx-9070-xt"),
    ("GIGABYTE GeForce RTX 5080 16GB GDDR6X PCIe Gaming Graphics Card with DisplayPort for Desktop", "rtx-5080"),
])
def test_cards_that_name_a_computer_are_still_cards(title, model_id):
    assert match(title, m(model_id)).ok
