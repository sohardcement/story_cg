"""Small, non-destructive ComfyUI HTTP client."""

import time
from pathlib import Path

import httpx


class ComfyClient:
    def __init__(self, base_url: str, timeout_seconds: float, *, transport=None):
        if timeout_seconds < 180:
            raise ValueError("ComfyUI timeout must be at least 180 seconds")
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_seconds,
                                 transport=transport, trust_env=False)

    def close(self):
        self.http.close()

    def upload_image(self, path: str | Path, *, image_type: str = "input", subfolder: str = "") -> dict:
        path = Path(path)
        with path.open("rb") as stream:
            response = self.http.post("/upload/image", files={"image": (path.name, stream, "image/png")},
                                      data={"type": image_type, "subfolder": subfolder, "overwrite": "false"})
        response.raise_for_status()
        return response.json()

    def submit_prompt(self, workflow: dict, client_id: str) -> str:
        response = self.http.post("/prompt", json={"prompt": workflow, "client_id": client_id})
        response.raise_for_status()
        result = response.json()
        if "prompt_id" not in result:
            raise ValueError(f"ComfyUI did not return prompt_id: {result}")
        return result["prompt_id"]

    def get_queue(self) -> dict:
        response = self.http.get("/queue")
        response.raise_for_status()
        return response.json()

    def get_history(self, prompt_id: str) -> dict:
        response = self.http.get(f"/history/{prompt_id}")
        response.raise_for_status()
        return response.json()

    def get_all_history(self) -> dict:
        response = self.http.get("/history")
        response.raise_for_status()
        return response.json()

    def wait_for_history(self, prompt_id: str, interval_seconds: float, timeout_seconds: float) -> dict | None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            record = self.get_history(prompt_id).get(prompt_id)
            if record is not None:
                return record
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(interval_seconds, remaining))

    def download_image(self, image_reference: dict) -> bytes:
        response = self.http.get("/view", params={
            "filename": image_reference["filename"],
            "subfolder": image_reference.get("subfolder", ""),
            "type": image_reference.get("type", "output"),
        })
        response.raise_for_status()
        return response.content
