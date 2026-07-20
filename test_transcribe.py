from faster_whisper import WhisperModel
import time

# We are using the "medium" model to balance speed and accuracy for noisy audio
model_size = "small"

print(f"Loading {model_size} model... (this may take a minute on the first run to download)")
# Since you are developing on a Mac, we specify "cpu" and "int8" for compatibility
model = WhisperModel(model_size, device="cpu", compute_type="int8")
print("Model loaded successfully!\n")

# Replace this with the actual name of your sample festival MP3
audio_file = "sample.mp3"

print(f"Transcribing {audio_file}...")
start_time = time.time()

# VAD is disabled for maximum capture, and language is locked to English for speed
segments, info = model.transcribe(audio_file, beam_size=5, vad_filter=False, language="en")

for segment in segments:
    print(f"[{segment.start:.2f}s -> {segment.end:.2f}s] {segment.text}")

end_time = time.time()
print(f"\nTranscription took {end_time - start_time:.2f} seconds.")