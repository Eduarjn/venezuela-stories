import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from efeitos import BEDS, MOTIONS, content_sfx, direct  # noqa: E402
from visual_generator import MOTION_TYPES, TRANSITIONS  # noqa: E402


def scenes(*texts, motion="slow_ken_burns"):
    return [{"scene_id": i, "narration_text": t, "motion_type": motion, "sfx_cue": "thunder"}
            for i, t in enumerate(texts, start=1)]


# Falas reais dos vídeos de 30/10 (avião) e 31/10 (ponte).
PONTE = scenes(
    "Um navio petroleiro derrubou parte de uma ponte gigante.",
    "Como um único navio conseguiu cortar uma ponte dessas ao meio?",
    "A ponte General Rafael Urdaneta cruza o Lago de Maracaibo, no oeste do país.",
    "Em 6 de abril de 1964, era a vez do petroleiro Esso Maracaibo.",
    "De repente... uma pane elétrica. O navio ficou sem energia.",
    "Sem energia, não conseguiu desviar. E bateu em cheio no pilar 31.",
    "Um trecho de mais de 200 metros despencou no lago.",
    "Carros que estavam sobre a ponte caíram na água.",
    "Sete pessoas morreram.",
    "Siga o canal para mais histórias reais da Venezuela.",
)


class DirectorTest(unittest.TestCase):
    def test_sound_follows_what_the_scene_tells(self):
        self.assertEqual(content_sfx("O piloto americano Jimmie Angel sobrevoa o sul da Venezuela.")[0], "plane")
        self.assertEqual(content_sfx("Só em 1970 um helicóptero o tirou lá de cima.")[0], "helicopter")
        self.assertEqual(content_sfx("No diário de bordo, ele anota.")[0], "writing")
        self.assertEqual(content_sfx("Uma única noite de raios pode durar nove horas.")[0], "thunder")
        self.assertEqual(content_sfx("Neste lago, o céu explode quase 300 noites por ano.")[0], "thunder")
        self.assertNotIn("thunder", content_sfx("Por semanas, nenhum raio."))  # negação: sem trovão

    def test_no_thunder_or_lightning_flash_without_a_storm(self):
        effects = direct(PONTE)
        self.assertNotIn("thunder", [e.sfx for e in effects])
        self.assertNotIn("flash", [e.transition for e in effects])
        self.assertEqual([e.sfx for e in effects][:2], ["impact", "ship_horn"])

    def test_nothing_repeats_in_consecutive_scenes(self):
        for case in (PONTE, scenes(*["Os raios caem sobre o lago."] * 12, motion="zoom_in_fast")):
            effects = direct(case)
            for a, b in zip(effects, effects[1:]):
                self.assertFalse(a.sfx and a.sfx == b.sfx, (a, b))
                self.assertFalse(a.transition and a.transition == b.transition, (a, b))
                self.assertNotEqual(a.motion, b.motion)

    def test_each_repetition_uses_another_recording(self):
        effects = direct(scenes(*["Os raios caem sobre o lago.", "Ninguém sabe por quê."] * 6))
        thunders = [e.sfx_variant for e in effects if e.sfx == "thunder"]
        self.assertEqual(thunders, list(range(len(thunders))))
        self.assertGreaterEqual(len(thunders), 3)  # vídeo de tempestade: o trovão aparece mais

    def test_death_gets_silence_and_a_dip_to_black(self):
        morte = direct(PONTE)[8]
        self.assertIsNone(morte.sfx)
        self.assertEqual(morte.transition, "dip")

    def test_time_jump_dips_and_ticks_when_nothing_else_fits(self):
        effects = direct(scenes("Tudo começou numa vila.", "Em 1937 ele volta decidido.", "Hoje ninguém lembra."))
        # "Hoje" também é salto no tempo, mas dois mergulhos seguidos viram corte seco.
        self.assertEqual([e.transition for e in effects], [None, "dip", None])
        self.assertEqual(effects[1].sfx, "clock")

    def test_impact_punches_and_shakes_and_falling_water_tilts_down(self):
        effects = direct(PONTE)
        batida = effects[5]
        self.assertEqual((batida.sfx, batida.transition, batida.motion), ("impact", "punch", "shake_impact"))
        queda = direct(scenes("Tudo começou.", "São 979 metros de queda."))[1]
        self.assertEqual(queda.motion, "tilt_down")

    def test_hook_starts_with_weight_and_call_to_action_is_quiet(self):
        effects = direct(scenes("Um avião ficou 33 anos preso numa montanha.", "Siga o canal para mais."))
        self.assertEqual(effects[0].sfx, "boom")   # avião seria ambiente: o gancho pede um impacto
        self.assertIsNone(effects[0].transition)
        self.assertIsNone(effects[1].sfx)

    def test_beds_appear_at_most_twice_and_most_cuts_are_plain(self):
        effects = direct(scenes(*[f"O avião número {i} pousa na pista." for i in range(10)]))
        self.assertLessEqual(sum(e.sfx == "plane" for e in effects), 2)
        self.assertIn("plane", BEDS)
        cuts = [e.transition for e in direct(PONTE)[1:]]
        self.assertGreaterEqual(cuts.count(None), len(cuts) // 2)

    def test_vocabulary_matches_the_renderer(self):
        self.assertEqual(set(MOTIONS), set(MOTION_TYPES))
        for case in (PONTE,):
            for effect in direct(case):
                self.assertTrue(effect.transition is None or effect.transition in TRANSITIONS)


if __name__ == "__main__":
    unittest.main()
