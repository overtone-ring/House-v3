"""
ComfyUI Service
================

Sends image generation prompts to a local ComfyUI instance.
Supports multiple workflow templates (e.g. realistic, anime).

Workflow JSONs are loaded once at init. Per-request, the service:
    1. Clones the workflow template
    2. Injects the user's prompt text and a random seed
    3. POSTs to ComfyUI's /prompt endpoint
    4. Polls /history for completion
    5. Downloads the output image from /view
    6. Returns the image path
"""

import asyncio
import copy
import json
import logging
import random
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)


# ── Workflow definitions ─────────────────────────────────────────────
# Maps style name → (workflow file, prompt node ID, seed node ID, negative node ID)
WORKFLOW_REGISTRY = {
    "realistic": {
        "file": "image_z_image_turbo.json",
        "prompt_node": "57:27",       # CLIPTextEncode — positive prompt
        "seed_node": "57:3",          # KSampler — seed field
        "negative_node": None,        # No negative prompt in this workflow
        "save_node": "9",             # SaveImage node
    },
    "anime": {
        "file": "image_anima_preview.json",
        "prompt_node": "11",          # CLIPTextEncode — positive prompt
        "seed_node": "19",            # KSampler — seed field
        "negative_node": "12",        # CLIPTextEncode — negative prompt
        "save_node": "46",            # SaveImage node
    },
}

# Default negative prompt for anime workflow
DEFAULT_NEGATIVE = "worst quality, low quality, score_1, score_2, score_3, blurry, jpeg artifacts, sepia"


class ComfyUIService:
    """
    Client for a local ComfyUI instance.

    Usage:
        service = ComfyUIService(config)
        image_path = await service.generate("a cat on a throne", style="realistic")
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self._config = config or {}
        comfy_config = self._config.get("comfyui", {})

        self._base_url = comfy_config.get("base_url", "http://127.0.0.1:8188")
        self._output_dir = Path(
            comfy_config.get("output_dir", tempfile.gettempdir())
        )
        self._poll_interval = comfy_config.get("poll_interval", 1.0)
        self._timeout = comfy_config.get("timeout", 120)

        # Load workflow templates
        workflows_dir = Path(comfy_config.get("workflows_dir", "."))
        self._workflows: Dict[str, dict] = {}

        for style, info in WORKFLOW_REGISTRY.items():
            workflow_path = workflows_dir / info["file"]
            if workflow_path.exists():
                with open(workflow_path) as f:
                    self._workflows[style] = json.load(f)
                logger.info(f"[ComfyUI] Loaded workflow: {style} ({workflow_path})")
            else:
                logger.warning(f"[ComfyUI] Workflow not found: {workflow_path}")

    @property
    def available_styles(self) -> list:
        return list(self._workflows.keys())

    async def generate(
        self,
        prompt: str,
        style: str = "realistic",
        negative: Optional[str] = None,
        seed: Optional[int] = None,
    ) -> Path:
        """
        Generate an image via ComfyUI.

        Args:
            prompt: The image generation prompt.
            style: "realistic" or "anime".
            negative: Optional negative prompt (anime only, has default).
            seed: Optional seed (random if not set).

        Returns:
            Path to the downloaded output image.
        """
        if style not in self._workflows:
            raise ValueError(
                f"Unknown style '{style}'. Available: {self.available_styles}"
            )

        workflow_info = WORKFLOW_REGISTRY[style]
        workflow = copy.deepcopy(self._workflows[style])

        # ── Inject prompt ────────────────────────────────────────
        prompt_node = workflow_info["prompt_node"]
        workflow[prompt_node]["inputs"]["text"] = prompt

        # ── Inject negative prompt (if applicable) ───────────────
        neg_node = workflow_info["negative_node"]
        if neg_node and neg_node in workflow:
            workflow[neg_node]["inputs"]["text"] = negative or DEFAULT_NEGATIVE

        # ── Inject seed ──────────────────────────────────────────
        seed_node = workflow_info["seed_node"]
        actual_seed = seed if seed is not None else random.randint(0, 2**53)
        workflow[seed_node]["inputs"]["seed"] = actual_seed

        logger.info(
            f"[ComfyUI] Generating: style={style}, seed={actual_seed}, "
            f"prompt={prompt[:60]}{'...' if len(prompt) > 60 else ''}"
        )

        # ── Submit to ComfyUI ────────────────────────────────────
        async with aiohttp.ClientSession() as session:
            # Queue the prompt
            prompt_id = await self._queue_prompt(session, workflow)

            # Poll for completion
            output_data = await self._wait_for_completion(session, prompt_id)

            # Download the image
            image_path = await self._download_image(session, output_data, style)

        logger.info(f"[ComfyUI] Image saved: {image_path}")
        return image_path

    # ── ComfyUI API ──────────────────────────────────────────────

    async def _queue_prompt(
        self, session: aiohttp.ClientSession, workflow: dict
    ) -> str:
        """POST the workflow to ComfyUI and return the prompt_id."""
        url = f"{self._base_url}/prompt"
        payload = {"prompt": workflow}

        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"ComfyUI rejected prompt: {resp.status} — {body[:200]}")
            data = await resp.json()
            prompt_id = data["prompt_id"]
            logger.debug(f"[ComfyUI] Queued: {prompt_id}")
            return prompt_id

    async def _wait_for_completion(
        self, session: aiohttp.ClientSession, prompt_id: str
    ) -> dict:
        """Poll /history/{prompt_id} until the job completes."""
        url = f"{self._base_url}/history/{prompt_id}"
        start = time.monotonic()

        while True:
            elapsed = time.monotonic() - start
            if elapsed > self._timeout:
                raise TimeoutError(
                    f"ComfyUI generation timed out after {self._timeout}s"
                )

            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if prompt_id in data:
                        history = data[prompt_id]
                        if history.get("status", {}).get("completed", False) or "outputs" in history:
                            return history
                        # Check for errors
                        status_msg = history.get("status", {}).get("status_str", "")
                        if "error" in status_msg.lower():
                            raise RuntimeError(f"ComfyUI generation failed: {status_msg}")

            await asyncio.sleep(self._poll_interval)

    async def _download_image(
        self, session: aiohttp.ClientSession, history: dict, style: str
    ) -> Path:
        """Download the generated image from ComfyUI's /view endpoint."""
        # Find the output image info from the history
        outputs = history.get("outputs", {})
        save_node = WORKFLOW_REGISTRY[style]["save_node"]

        images = None
        if save_node in outputs:
            images = outputs[save_node].get("images", [])

        # Fallback: search all output nodes for images
        if not images:
            for node_id, node_output in outputs.items():
                if "images" in node_output and node_output["images"]:
                    images = node_output["images"]
                    break

        if not images:
            raise RuntimeError("ComfyUI returned no output images")

        image_info = images[0]  # Take the first image
        filename = image_info["filename"]
        subfolder = image_info.get("subfolder", "")
        img_type = image_info.get("type", "output")

        # Download via /view
        params = {"filename": filename, "subfolder": subfolder, "type": img_type}
        url = f"{self._base_url}/view"

        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Failed to download image: {resp.status}")

            # Save to output dir
            self._output_dir.mkdir(parents=True, exist_ok=True)
            output_path = self._output_dir / filename

            with open(output_path, "wb") as f:
                f.write(await resp.read())

        return output_path

    # ── Health Check ─────────────────────────────────────────────

    async def is_available(self) -> bool:
        """Check if ComfyUI is running and reachable."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._base_url}/system_stats", timeout=aiohttp.ClientTimeout(total=3)
                ) as resp:
                    return resp.status == 200
        except Exception as e:
            logger.debug(f"ComfyUI availability check failed: {e}")
            return False

    def __repr__(self) -> str:
        return (
            f"ComfyUIService(url={self._base_url}, "
            f"styles={self.available_styles})"
        )
