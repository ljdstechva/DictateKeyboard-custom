/*
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package dev.patrickgold.florisboard.dictate.data.prompts

import io.kotest.core.spec.style.FunSpec
import io.kotest.matchers.shouldBe

// Custom build: what an offline Whisper model writes for a quiet recording must not be typed.
class NonSpeechTagTest : FunSpec({

    test("Whisper's non-speech annotations alone count as no speech") {
        listOf(
            "[BLANK_AUDIO]", " [ Pause ] ", "[BLANK_AUDIO] [BLANK_AUDIO]", "(silence)", "[Music].",
            "[ Silence ]\n", "[no speech]", "(Inaudible)",
        ).forEach { DictatePromptDefaults.looksLikeNonSpeechOnly(it) shouldBe true }
    }

    test("real text, even next to an annotation, is kept") {
        listOf(
            "", "Hello world", "Hello [BLANK_AUDIO]", "[Best regards]", "(laughs)", "pause the music",
            "[BLANK_AUDIO] ok",
        ).forEach { DictatePromptDefaults.looksLikeNonSpeechOnly(it) shouldBe false }
    }
})
