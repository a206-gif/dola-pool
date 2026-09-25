import re
import requests

AGENS_API_KEY = "sk-VzHjFqQGpBRsb5mTbIufAR2NgnHR1xDn1oO8U1xLFd9szNK8"
AGENS_BASE_URL = "https://apihub.agnes-ai.com/v1"
AGENS_MODEL = "agnes-3.0-flash"

def ai_reason_script(script_text, style="movie quality", duration=15, image_url=""):
    try:
        system_prompt = "You are a professional AI video storyboard assistant. Generate high-quality video prompts based on the user script. Each scene one prompt, 100-200 words each. Output only the prompts, one per line. No explanations."
        messages = [{"role": "system", "content": system_prompt}]
        
        if image_url:
            img_text = "Generate video storyboard prompts based on this image and script. Script: " + script_text + " Style: " + style + " Duration: " + str(duration) + "s per scene. Output one prompt per line."
            user_content = [{"type": "text", "text": img_text}, {"type": "image_url", "image_url": {"url": image_url}}]
            messages.append({"role": "user", "content": user_content})
        else:
            text_msg = "Generate video storyboard prompts. Script: " + script_text + " Style: " + style + " Duration: " + str(duration) + "s per scene. Output one prompt per line."
            messages.append({"role": "user", "content": text_msg})
        
        r = requests.post(
            AGENS_BASE_URL + "/chat/completions",
            headers={"Authorization": "Bearer " + AGENS_API_KEY, "Content-Type": "application/json"},
            json={"model": AGENS_MODEL, "messages": messages, "temperature": 0.7, "max_tokens": 2000},
            timeout=60
        )
        
        if r.status_code != 200:
            return None, "AI failed: " + r.text[:200]
        
        result = r.json()
        ai_content = result["choices"][0]["message"]["content"]
        
        lines = ai_content.split("\n")
        cleaned = []
        for line in lines:
            line = line.strip()
            if len(line) > 10:
                line = re.sub(r"^\d+[\.\、\:\：]\s*", "", line)
                line = re.sub(r"^Scene_\d+[\:\：]\s*", "", line)
                if len(line) > 10:
                    cleaned.append(line)
        
        if not cleaned:
            cleaned = [ai_content[:200]]
        
        scenes = []
        for i, prompt in enumerate(cleaned[:15]):
            scenes.append({"title": "Scene_" + str(i+1), "script": "", "prompt": prompt, "duration": duration, "status": "ready"})
        
        return scenes, None
    except Exception as e:
        return None, "AI error: " + str(e)
