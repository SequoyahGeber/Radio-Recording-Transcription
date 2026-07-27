import re
from collections import Counter


WORD_PATTERN = re.compile(r"[A-Za-z0-9']+")
NON_SPEECH_ONLY = re.compile(
    r"[\[(](?:blank[_ ]audio|silence|music|no speech|inaudible)[\])]",
    re.IGNORECASE,
)
COMMON_HALLUCINATIONS = {
    "thank you",
    "thanks for watching",
    "please subscribe",
    "subscribe",
    "you",
}


def normalize_words(text):
    return [word.lower().strip("'") for word in WORD_PATTERN.findall(text or "")]


def _longest_identical_run(words):
    longest = 0
    current = 0
    previous = None
    for word in words:
        if word == previous:
            current += 1
        else:
            current = 1
            previous = word
        longest = max(longest, current)
    return longest


def _dominant_ngram_ratio(words, size):
    if len(words) < size:
        return 0.0, 0
    ngrams = [tuple(words[index : index + size]) for index in range(len(words) - size + 1)]
    _, occurrences = Counter(ngrams).most_common(1)[0]
    covered = min(len(words), occurrences * size)
    return covered / len(words), occurrences


def assess_transcript(text, audio_duration=None, engine_metrics=None):
    cleaned = NON_SPEECH_ONLY.sub("", (text or "").strip())
    words = normalize_words(cleaned)
    metrics = dict(engine_metrics or {})
    metrics["word_count"] = len(words)
    metrics["unique_word_count"] = len(set(words))

    if not words:
        return {
            "status": "blank",
            "score": 0.0,
            "reason": "No speech was detected",
            "metrics": metrics,
        }

    counts = Counter(words)
    dominant_word, dominant_count = counts.most_common(1)[0]
    dominant_ratio = dominant_count / len(words)
    longest_run = _longest_identical_run(words)
    bigram_ratio, bigram_occurrences = _dominant_ngram_ratio(words, 2)
    trigram_ratio, trigram_occurrences = _dominant_ngram_ratio(words, 3)
    unique_ratio = len(counts) / len(words)
    metrics.update(
        {
            "dominant_word": dominant_word,
            "dominant_ratio": round(dominant_ratio, 3),
            "longest_repeat_run": longest_run,
            "unique_ratio": round(unique_ratio, 3),
            "bigram_repeat_ratio": round(bigram_ratio, 3),
            "trigram_repeat_ratio": round(trigram_ratio, 3),
        }
    )

    reasons = []
    penalty = 0.0
    normalized_phrase = " ".join(words)
    if normalized_phrase in COMMON_HALLUCINATIONS and (audio_duration or 0) >= 2:
        reasons.append("Known Whisper hallucination phrase")
        penalty += 0.8
    if len(words) >= 4 and dominant_ratio >= 0.7:
        reasons.append("One word dominates the transcript")
        penalty += 0.75
    if longest_run >= 4:
        reasons.append("A word repeats four or more times in a row")
        penalty += 0.75
    if len(words) >= 8 and unique_ratio <= 0.25:
        reasons.append("Very low vocabulary diversity")
        penalty += 0.65
    if bigram_occurrences >= 3 and bigram_ratio >= 0.65:
        reasons.append("A two-word phrase repeats throughout")
        penalty += 0.7
    if trigram_occurrences >= 3 and trigram_ratio >= 0.65:
        reasons.append("A phrase repeats throughout")
        penalty += 0.7

    compression_ratio = metrics.get("compression_ratio")
    if compression_ratio is not None and compression_ratio > 2.4:
        reasons.append("Decoder reported excessive repetition")
        penalty += 0.5
    average_log_probability = metrics.get("avg_logprob")
    if average_log_probability is not None and average_log_probability < -1.0:
        reasons.append("Decoder confidence was low")
        penalty += 0.35
    no_speech_probability = metrics.get("no_speech_prob")
    if no_speech_probability is not None and no_speech_probability > 0.75:
        reasons.append("Decoder reported probable silence")
        penalty += 0.45

    score = max(0.0, min(1.0, 1.0 - penalty))
    return {
        "status": "suspect" if score < 0.45 else "ready",
        "score": round(score, 3),
        "reason": "; ".join(dict.fromkeys(reasons)),
        "metrics": metrics,
    }


def has_meaningful_transcript(text):
    return assess_transcript(text)["status"] != "blank"
