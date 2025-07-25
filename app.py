import gradio as gr
import spaces
import torch
from diffusers import AutoencoderKL, TCDScheduler, DPMSolverMultistepScheduler
from diffusers.models.model_loading_utils import load_state_dict
from gradio_imageslider import ImageSlider
from huggingface_hub import hf_hub_download
from PIL import ImageDraw, ImageFont, Image

from controlnet_union import ControlNetModel_Union
from pipeline_fill_sd_xl import StableDiffusionXLFillPipeline

MODELS = {
    "RealVisXL V5.0 Lightning": "SG161222/RealVisXL_V5.0_Lightning",
}

# Set device for Apple Silicon (M4) Core ML/MPS
if torch.backends.mps.is_available():
    device = torch.device('mps')
else:
    device = torch.device('cpu')
dtype = torch.float32  # Core ML/MPS prefers float32

config_file = hf_hub_download(
    "xinsir/controlnet-union-sdxl-1.0",
    filename="config_promax.json",
)

config = ControlNetModel_Union.load_config(config_file)
controlnet_model = ControlNetModel_Union.from_config(config)
model_file = hf_hub_download(
    "xinsir/controlnet-union-sdxl-1.0",
    filename="diffusion_pytorch_model_promax.safetensors",
)
state_dict = load_state_dict(model_file)
model, _, _, _, _ = ControlNetModel_Union._load_pretrained_model(
    controlnet_model, state_dict, model_file, "xinsir/controlnet-union-sdxl-1.0"
)
model.to(device=device, dtype=dtype)

vae = AutoencoderKL.from_pretrained(
    "madebyollin/sdxl-vae-fp16-fix", torch_dtype=dtype
).to(device)

pipe = StableDiffusionXLFillPipeline.from_pretrained(
    "SG161222/RealVisXL_V5.0_Lightning",
    torch_dtype=dtype,
    vae=vae,
    controlnet=model,
    # Remove variant for Core ML/MPS
).to(device)

pipe.scheduler = TCDScheduler.from_config(pipe.scheduler.config)

def add_watermark(image, text="ProFaker", font_path="BRLNSDB.TTF", font_size=25):
    # Load the Berlin Sans Demi font with the specified size
    font = ImageFont.truetype(font_path, font_size)

    # Position the watermark in the bottom right corner, adjusting for text size
    text_bbox = font.getbbox(text)
    text_width, text_height = text_bbox[2], text_bbox[3]
    watermark_position = (image.width - text_width - 100, image.height - text_height - 150)

    # Draw the watermark text with a translucent white color
    draw = ImageDraw.Draw(image)
    draw.text(watermark_position, text, font=font, fill=(255, 255, 255, 150))  # RGBA for transparency

    return image

@spaces.GPU
def fill_image(prompt, negative_prompt, image, model_selection, paste_back, guidance_scale, num_steps):
    (
        prompt_embeds,
        negative_prompt_embeds,
        pooled_prompt_embeds,
        negative_pooled_prompt_embeds,
    ) = pipe.encode_prompt(prompt, device, True, negative_prompt=negative_prompt)

    source = image["background"]
    mask = image["layers"][0]

    alpha_channel = mask.split()[3]
    binary_mask = alpha_channel.point(lambda p: p > 0 and 255)
    cnet_image = source.copy()
    cnet_image.paste(0, (0, 0), binary_mask)

    for image in pipe(
        prompt_embeds=prompt_embeds,
        negative_prompt_embeds=negative_prompt_embeds,
        pooled_prompt_embeds=pooled_prompt_embeds,
        negative_pooled_prompt_embeds=negative_pooled_prompt_embeds,
        image=cnet_image,
        guidance_scale=guidance_scale,
        num_inference_steps=num_steps,
    ):
        yield image, cnet_image

    print(f"{model_selection=}")
    print(f"{paste_back=}")

    if paste_back:
        image = image.convert("RGBA")
        cnet_image.paste(image, (0, 0), binary_mask)
    else:
        cnet_image = image

    cnet_image = add_watermark(cnet_image)
    yield source, cnet_image


def clear_result():
    return gr.update(value=None)


title = """<h1 align="center">ProFaker</h1>"""

with gr.Blocks() as demo:
    gr.HTML(title)
    with gr.Row():
        with gr.Column():
            prompt = gr.Textbox(
                label="Prompt",
                info="Describe what to inpaint the mask with",
                lines=3,
            )
            
            with gr.Accordion("Advanced Options", open=False):
                negative_prompt = gr.Textbox(
                    label="Negative Prompt",
                    info="Describe what you dont want in the mask",
                    lines=3,
                )
                guidance_scale = gr.Slider(
                    minimum=1,
                    maximum=10,
                    value=1.5,
                    step=0.1,
                    label="Guidance Scale"
                )
                num_steps = gr.Slider(
                    minimum=5,
                    maximum=100,
                    value=10,
                    step=1,
                    label="Steps"
                )
            
            input_image = gr.ImageMask(
                type="pil", label="Input Image",crop_size=(1200,1200), layers=False
            )
        with gr.Column():
            model_selection = gr.Dropdown(
                choices=list(MODELS.keys()),
                value="RealVisXL V5.0 Lightning",
                label="Model",
            )

            with gr.Row():
                with gr.Column():
                    run_button = gr.Button("Generate")

                with gr.Column():
                    paste_back = gr.Checkbox(True, label="Paste back original")

            result = ImageSlider(
                interactive=False,
                label="Generated Image",
                type="pil"
            )

    use_as_input_button = gr.Button("Use as Input Image", visible=False)

    def use_output_as_input(output_image):
        return gr.update(value=output_image[1])

    use_as_input_button.click(
        fn=use_output_as_input, inputs=[result], outputs=[input_image]
    )

    run_button.click(
        fn=clear_result,
        inputs=None,
        outputs=result,
    ).then(
        fn=lambda: gr.update(visible=False),
        inputs=None,
        outputs=use_as_input_button,
    ).then(
        fn=fill_image,
        inputs=[prompt, negative_prompt, input_image, model_selection, paste_back, guidance_scale, num_steps],
        outputs=result,
    ).then(
        fn=lambda: gr.update(visible=True),
        inputs=None,
        outputs=use_as_input_button,
    )

    prompt.submit(
        fn=clear_result,
        inputs=None,
        outputs=result,
    ).then(
        fn=lambda: gr.update(visible=False),
        inputs=None,
        outputs=use_as_input_button,
    ).then(
        fn=fill_image,
        inputs=[prompt, negative_prompt, input_image, model_selection, paste_back, guidance_scale, num_steps],
        outputs=result,
    ).then(
        fn=lambda: gr.update(visible=True),
        inputs=None,
        outputs=use_as_input_button,
    )


demo.queue(max_size=12).launch(share=False)
