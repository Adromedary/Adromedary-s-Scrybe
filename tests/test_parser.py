import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scrybe as s

def check(text, expect):
    got = [(e.qty, e.name, e.set, e.number, e.section) for e in s.parse_decklist(text)]
    assert got == expect, f"\nINPUT: {text!r}\nGOT:    {got}\nEXPECT: {expect}"

check("1 Sol Ring\n31 Swamp\n\n// Commander\n1 Gisa, the Hellraiser\n\n// Outside the Game\n",
      [(1, "Sol Ring", None, None, "main"), (31, "Swamp", None, None, "main"),
       (1, "Gisa, the Hellraiser", None, None, "commander")])
# Arena / Moxfield
check("Commander\n1 Krenko, Mob Boss (DDT) 52\n\nDeck\n4x Lightning Bolt (2X2) 117 *F*\n1 Delver of Secrets // Insectile Aberration (ISD) 51\nSideboard\n1 Negate",
      [(1, "Krenko, Mob Boss", "ddt", "52", "commander"), (4, "Lightning Bolt", "2x2", "117", "main"),
       (1, "Delver of Secrets // Insectile Aberration", "isd", "51", "main"), (1, "Negate", None, None, "sideboard")])
# Archidekt export with categories & tags
check("1x Sol Ring (c21) 263 [Ramp] ^Have,#37d67a^\n1x Brainstorm (sta) 13 [Maybeboard{noDeck}{noPrice}]\n1x Atraxa, Grand Unifier (one) 196 [Commander{top}]",
      [(1, "Sol Ring", "c21", "263", "main"), (1, "Brainstorm", "sta", "13", "maybe"),
       (1, "Atraxa, Grand Unifier", "one", "196", "commander")])
# Forge .dck
check("[metadata]\nName=Test\n[Commander]\n1 Omnath, Locus of Creation|ZNR\n[Main]\n16 Forest|ZNR|1\n",
      [(1, "Omnath, Locus of Creation", "znr", None, "commander"), (16, "Forest", "znr", None, "main")])
# set without number, bracket set, merging, headers with counts
check("// Creatures (2)\n16 Forest (ZNR)\n2 Forest [KHM]\n4 Forest\n4 Forest\nMaybeboard\n1 Fog",
      [(16, "Forest", "znr", None, "main"), (2, "Forest", "khm", None, "main"),
       (8, "Forest", None, None, "main"), (1, "Fog", None, None, "maybe")])
# MTGO .dek
check('<?xml version="1.0"?><Deck><Cards CatID="1" Quantity="4" Sideboard="false" Name="Island" /><Cards CatID="2" Quantity="1" Sideboard="true" Name="Negate" /></Deck>',
      [(4, "Island", None, None, "main"), (1, "Negate", None, None, "sideboard")])
# Commander List.txt is not a deck
assert not s.looks_like_decklist("Commander List:\n\n1B - The Dead Don't Die (Gisa, the Hellraiser)\n5WUBRG - Keeper of Tales (Tom Bombadil)")
assert s.looks_like_decklist("1 Sol Ring")
# normalisation
assert s.norm("Haldir, Lórien Lieutenant") == s.norm("Haldir, Lorien Lieutenant")
assert s.norm("Fire/Ice") == s.norm("Fire // Ice")
assert s.norm("Lim-Dûl’s Vault") == s.norm("Lim-Dul's Vault")
assert s.safe_filename("Fire // Ice") == "Fire - Ice"
print("parser tests passed")
