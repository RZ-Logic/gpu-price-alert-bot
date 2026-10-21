"""Adversarial titles from the 2026-10-12 code review: 100 listings that should all be rejected.

Each pair is (catalog model id, title). The review found 51 accepted by the matcher as first built;
after the fixes, the count printed below is what remains. Run from the repo root:

    python eval/adversarial_titles.py

This measures match() alone. In the bot, the price floor and each retailer's category filter
stop many of these before they could alert. tests/test_eval.py pins which ones are still
accepted, so a matcher change that moves the count updates that test in the same diff.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gpu_alerts import catalog
from gpu_alerts.matching import match, parse_title

# (model_id, title) pairs that should all be REJECTED (expected False)
cases = [
 # suffix shorthand
 ("rtx-4080", "ASUS TUF Gaming GeForce RTX 4080S OC 16GB GDDR6X Graphics Card"),
 ("rtx-4080", "MSI GeForce RTX 4080 S 16GB Gaming X Slim Graphics Card"),
 ("rtx-4070", "Zotac RTX 4070S Twin Edge 12GB GDDR6X Graphics Card"),
 ("rtx-4070-ti", "Gigabyte RTX 4070 Ti S 16GB Eagle OC Graphics Card"),
 ("rtx-4070-ti", "RTX 4070 Ti-S 16GB GDDR6X Graphics Card"),
 ("rtx-5070", "ASUS RTX5070T 16GB Graphics Card"),
 ("rtx-5070", "ZOTAC GAMING GeForce RTX 5070 Ti SOLID 16GB GDDR7 ZT-B50710J-10P"),
 ("rtx-5070", "GeForce RTX 5070-Ti 16GB Graphics Card"),
 ("rtx-5070", "GeForce RTX 5070 T.I. 16GB Graphics Card"),
 ("rtx-5070", "GeForce RTX 5070 Super 18GB Graphics Card"),
 ("rtx-5080", "GeForce RTX 5080 Super 24GB GDDR7 Graphics Card"),
 ("rx-9070", "XFX Swift Radeon RX 9070XT 16GB Graphics Card"),
 ("rx-9070", "PowerColor Radeon RX 9070 GRE 12GB Graphics Card"),
 ("rx-9070", "ASRock Radeon RX 9070 X T Steel Legend 16GB Graphics Card"),
 ("rx-9070", "Sapphire Pulse Radeon RX 9070-XT 16GB Graphics Card"),
 ("rx-7900-xt", "XFX Radeon RX 7900 XTX 24GB Graphics Card"),
 ("rx-7900-xt", "Radeon RX 7900XT X 24GB"),
 ("rx-9060", "Radeon RX 9060 XT 8GB Graphics Card"),
 ("rx-7600", "Radeon RX 7600 XT 16GB Graphics Card"),
 ("rx-7600", "ASRock AMD Radeon RX 7600XT Challenger 16GB"),
 ("rtx-5060", "MSI GeForce RTX 5060 Ti 8GB Ventus 2X Graphics Card"),
 ("rtx-5060", "GIGABYTE GeForce RTX 5060 Ti Graphics Card GV-N506TWF2OC-8GD"),
 ("rtx-5060", "GeForce RTX 5060 Ti 16G"),
 ("arc-b580", "Intel Arc A580 8GB Graphics Card"),
 ("arc-b580", "Intel Arc Pro B580 24GB"),
 ("arc-b570", "Sparkle Intel Arc B580 Titan OC 12GB"),
 # China / workstation
 ("rtx-5090", "NVIDIA GeForce RTX 5090D 32GB Graphics Card"),
 ("rtx-5090", "ASUS ROG Astral RTX 5090 D V2 24GB Graphics Card"),
 ("rtx-5090", "GeForce RTX 5090DD 32GB Graphics Card"),
 ("rtx-5090", "NVIDIA RTX PRO 6000 Blackwell 96GB vs RTX 5090"),
 ("rtx-4090", "NVIDIA GeForce RTX 4090D 24GB Graphics Card"),
 ("rtx-4090", "MSI GeForce RTX 4090 D 24G Gaming X Slim"),
 ("rtx-5090", "NVIDIA RTX 5090 Laptop GPU"),
 ("rtx-4090", "PNY NVIDIA RTX 4000 SFF Ada Generation 20GB Graphics Card"),
 ("rtx-5080", "NVIDIA RTX PRO 5000 Blackwell 48GB Graphics Card"),
 ("rtx-5080", "Quadro RTX 5000 16GB Graphics Card"),
 # VRAM
 ("rtx-5060-ti-16gb", "MSI GeForce RTX 5060 Ti 8GB/16GB Gaming OC Graphics Card"),
 ("rtx-5060-ti-16gb", "GeForce RTX 5060 Ti 8GB GDDR7 (16GB version also available)"),
 ("rtx-5060-ti-16gb", "GeForce RTX 5060 Ti Graphics Card 8GB GDDR7, 16 GB/s"),
 ("rtx-5060-ti-16gb", "GeForce RTX 5060 Ti Gaming Graphics Card, 16GB DDR5 System Recommended, 8GB GDDR7"),
 ("rtx-5060-ti-16gb", "ASUS Dual GeForce RTX 5060 Ti OC 8GB GDDR7 128-bit 16 Gbps"),
 ("rtx-5060-ti-8gb", "ASUS Dual GeForce RTX 5060 Ti OC 16GB GDDR7 (8-Pin)"),
 ("rtx-5060-ti-8gb", "GeForce RTX 5060 Ti 16 GB, 8 GB/s"),
 ("rtx-5060-ti-16gb", "GIGABYTE RTX 5060 Ti 16GB vs 8GB"),
 ("rtx-5060-ti-16gb", "RTX 5060 Ti 8G 16G"),
 ("rx-9060-xt-16gb", "Sapphire Pulse Radeon RX 9060 XT 8GB GDDR6 16Gbps"),
 ("rx-9060-xt-16gb", "PowerColor Reaper RX 9060 XT 8GB/16GB"),
 ("rx-9060-xt-16gb", "XFX Radeon RX 9060 XT 8 Go GDDR6"),
 ("rtx-4060-ti-16gb", "MSI RTX 4060 Ti Ventus 2X 8G OC, supports 16GB system"),
 ("rx-9060-xt-16gb", "ASRock Radeon RX 9060 XT Challenger 8GB OC, PCIe 5.0 x16"),
 ("rtx-5060-ti-16gb", "Zotac RTX 5060 Ti Twin Edge 8GB GDDR7 PCIe 5.0 x16"),
 ("rtx-5060-ti-16gb", "Zotac RTX 5060 Ti 8GB Twin Edge 16 GB"),
 # multi model / bundles
 ("rtx-5070-ti", "MSI RTX 5070 12G Ventus (RTX 5070 Ti cooler design)"),
 ("rtx-5070-ti", "Lot of 2: RTX 5070 Ti and RTX 5070"),
 ("rx-9070-xt", "AMD Radeon RX 9070 Graphics Card - beats RX 9070 XT"),
 ("rtx-5080", "MSI RTX 5080 16G Ventus + MSI MAG A850GL 850W"),
 ("rtx-5080", "MSI RTX 5080 16G Ventus + Corsair RM1000x 1000W Bundle"),
 ("rtx-5080", "ASUS RTX 5080 Prime + ROG Strix B850-F Gaming WiFi"),
 ("rtx-5080", "RTX 5080 16GB Graphics Card + AMD Ryzen 7 9800X3D"),
 ("rtx-5080", "RTX 5080 16GB Graphics Card Combo with 27\" 4K display"),
 ("rtx-5090", "2-Pack NVIDIA RTX 5090 32GB Graphics Card"),
 # prebuilts / laptops
 ("rtx-5080", "iBUYPOWER Y40 Gaming PC Desktop, NVIDIA GeForce RTX 5080 16GB GDDR7 Graphics"),
 ("rtx-5080", "Skytech Gaming Desktop - RTX 5080 16GB GPU"),
 ("rtx-5070-ti", "CyberPowerPC Gamer Xtreme VR - RTX 5070 Ti 16GB GPU, 32GB, 2TB"),
 ("rtx-5070-ti", "HP OMEN 35L Gaming Desktop, GeForce RTX 5070 Ti 16GB Graphics"),
 ("rtx-5070-ti", "Lenovo Legion Tower 5 Gen 10 with RTX 5070 Ti GPU"),
 ("rtx-5090", "Alienware Area-51 Gaming Desktop RTX 5090 32GB GDDR7"),
 ("rtx-5090", "MSI MEG Trident X2 RTX 5090 Graphics"),
 ("rtx-5070", "ASUS ROG Strix G16 (2025) RTX 5070 Ti 16\" display"),
 ("rtx-5070", "Razer Blade 16 RTX 5070 Ti 12GB"),
 ("rtx-5060", "Acer Nitro 16 Gaming RTX 5060 8GB"),
 ("rtx-5060", "Dell 16 Plus RTX 5060 8GB GDDR7"),
 ("rtx-5060", "Lenovo LOQ 15 RTX 5060 8GB GDDR7"),
 ("rtx-5070", "MSI Sword 16 HX RTX 5070 8GB GDDR7"),
 ("rtx-5070", "Gigabyte AORUS Elite 16 RTX 5070 8GB"),
 ("rtx-5060", "HP OMEN 16 RTX 5060 8GB GDDR7"),
 ("rtx-5060", "ASUS TUF Gaming A16 RTX 5060 8GB"),
 ("rtx-5060", "ASUS TUF Gaming F16 RTX 5060 8GB"),
 ("rtx-5070", "ASUS ROG Flow Z13 RTX 5070 8GB"),
 ("rtx-5060", "Acer Predator Helios Neo 16S RTX 5060"),
 ("rtx-5060", "Lenovo Legion Pro 5 RTX 5060"),
 # accessories and junk
 ("rtx-5090", "EKWB EK-Quantum Vector2 RTX 5090 Water-Block"),
 ("rtx-5090", "Bykski RTX 5090 GPU Liquid Cooling Block"),
 ("rtx-5090", "NVIDIA GeForce RTX 5090 Founders Edition - Empty Box Only"),
 ("rtx-5090", "NVIDIA GeForce RTX 5090 Founders Edition 32GB GDDR7 (For Parts, Not Working)"),
 ("rtx-5090", "ASUS ROG Astral RTX 5090 32GB - Broken, Defective, As-Is"),
 ("rtx-5090", "RTX 5090 32GB GDDR7 Graphics Card Repair Service"),
 ("rtx-5090", "NVIDIA RTX 5090 32GB Graphics Card Rental (per month)"),
 ("rtx-5090", "RTX 5090 12VHPWR 16-pin Power Connector Graphics Card"),
 ("rtx-5090", "Alphacool Core RTX 5090 Reference GPU Cooler with Backplate"),
 ("rtx-5090", "Corsair 12V-2x6 PCIe 5.1 GPU Power Cord RTX 5090"),
 ("rtx-5090", "RTX 5090 Graphics Card Support Pillar"),
 ("rtx-5090", "GPU Backplate RTX 5090 32GB"),
 ("rtx-5080", "MSI RTX 5080 Gaming Trio OC 16G box and manual"),
 ("rtx-5070", "Graphics Card Fan Replacement for RTX 5070"),
 ("arc-b580", "Intel B580 Motherboard"),
 ("rtx-5090", "NVIDIA GeForce RTX 5090 Founders Edition (Used)"),
 ("rtx-5090", "NVIDIA GeForce RTX 5090 Founders Edition 32GB GDDR7 - Used, Like New"),
 ("rtx-5090", "Gaming PC Case with RTX 5090 32GB Graphics Card Support"),
 ("rtx-5090", "ASUS ROG Astral RTX 5090 32GB GDDR7 Gaming Graphics Card Gift Card"),
]


def false_accepts() -> list[tuple[str, str]]:
    """The (model_id, title) cases that match() still accepts."""
    models = {m.id: m for m in catalog.load()}
    return [(mid, title) for mid, title in cases if match(title, models[mid]).ok]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    M = {m.id: m for m in catalog.load()}
    fp = 0
    for mid, title in cases:
        r = match(title, M[mid])
        p = parse_title(title)
        mark = "FP " if r.ok else "ok "
        fp += r.ok
        print(f"{mark}[{mid}] {title!r} -> {r.reason} | kind={p.kind.value} models={p.models} vram={p.vram_gb}")
    print("false accepts:", fp, "of", len(cases))


if __name__ == "__main__":
    main()
