// Phase Eight evidence-gated transcription experiments and channel profiles.
const phaseEightState = { profiles: new Map(), gate: null };

async function phaseEightJson(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "Channel profile request failed");
  return payload;
}

function phaseEightRenderProfile() {
  const channel = document.getElementById("transcription-profile-channel").value;
  const profile = phaseEightState.profiles.get(channel) || {};
  document.getElementById("profile-normalize-audio").checked = Boolean(profile.normalize_audio);
  document.getElementById("profile-language-detect").checked = profile.language_mode === "detect";
  document.getElementById("profile-denoise-audio").checked = Boolean(profile.denoise_audio);
  const eligible = Boolean(phaseEightState.gate?.eligible);
  document.getElementById("profile-language-detect").disabled = !eligible;
  document.getElementById("profile-denoise-audio").disabled = !eligible;
  document.getElementById("profile-evaluation-gate").textContent =
    phaseEightState.gate?.explanation || "Loading evaluation gate…";
}

async function phaseEightLoad() {
  const payload = await phaseEightJson("/api/quality-analytics");
  phaseEightState.profiles = new Map(
    (payload.profiles || []).map((profile) => [profile.channel, profile]),
  );
  phaseEightState.gate = payload.analytics.evaluation_gate;
  const glossaryChannels = document.getElementById("glossary-channel");
  if (!glossaryChannels.options.length) {
    window.setTimeout(() => phaseEightLoad().catch(() => {}), 500);
    return;
  }
  const select = document.getElementById("transcription-profile-channel");
  const previous = select.value;
  select.replaceChildren(
    ...[...glossaryChannels.options].map((option) => {
      const clone = document.createElement("option");
      clone.value = option.value;
      clone.textContent = option.textContent;
      return clone;
    }),
  );
  if ([...select.options].some((option) => option.value === previous)) select.value = previous;
  phaseEightRenderProfile();
}

async function phaseEightSave() {
  const channel = document.getElementById("transcription-profile-channel").value;
  if (!channel) return;
  const languageDetection = document.getElementById("profile-language-detect").checked;
  const denoise = document.getElementById("profile-denoise-audio").checked;
  try {
    const profile = await phaseEightJson(
      `/api/transcription-profiles/${encodeURIComponent(channel)}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          normalize_audio: document.getElementById("profile-normalize-audio").checked,
          denoise_audio: denoise,
          language_mode: languageDetection ? "detect" : "explicit",
          experimental_enabled: languageDetection || denoise,
        }),
      },
    );
    phaseEightState.profiles.set(channel, profile);
    showToast(`Transcription profile saved for ${channel}.`, "success");
  } catch (error) {
    showToast(error.message, "danger");
  }
}

function phaseEightBoot() {
  if (!currentProfile) {
    window.setTimeout(phaseEightBoot, 100);
    return;
  }
  if (!currentProfile.permissions?.profiles) return;
  window.setTimeout(() => phaseEightLoad().catch(() => {}), 500);
}

document
  .getElementById("transcription-profile-channel")
  .addEventListener("change", phaseEightRenderProfile);
document
  .getElementById("transcription-profile-save")
  .addEventListener("click", phaseEightSave);
phaseEightBoot();
