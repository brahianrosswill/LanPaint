from contextlib import contextmanager
from inspect import cleandoc
import inspect
# import nodes.py
import comfy
import nodes
import latent_preview
from functools import partial
from comfy.utils import repeat_to_batch_size
from comfy.samplers import *
from comfy.model_base import ModelType
from .utils import *
from .lanpaint import LanPaint


def reshape_mask(input_mask, output_shape):
    dims = len(output_shape) - 2


    scale_mode = "nearest-exact"
    mask = torch.nn.functional.interpolate(input_mask, size=output_shape[2:], mode=scale_mode)
    if mask.shape[1] < output_shape[1]:
        mask = mask.repeat((1, output_shape[1]) + (1,) * dims)[:,:output_shape[1]]
    mask = repeat_to_batch_size(mask, output_shape[0])
    return mask
def prepare_mask(noise_mask, shape, device):
    return reshape_mask(noise_mask, shape).to(device)
def sampling_function_LanPaint(model, x, timestep, uncond, cond, cond_scale, cond_scale_BIG, model_options={}, seed=None):
    if math.isclose(cond_scale, 1.0) and model_options.get("disable_cfg1_optimization", False) == False:
        uncond_ = None
    else:
        uncond_ = uncond

    conds = [cond, uncond_]
    out = calc_cond_batch(model, conds, x, timestep, model_options)

    for fn in model_options.get("sampler_pre_cfg_function", []):
        args = {"conds":conds, "conds_out": out, "cond_scale": cond_scale, "timestep": timestep,
                "input": x, "sigma": timestep, "model": model, "model_options": model_options}
        out  = fn(args)

    return cfg_function(model, out[0], out[1], cond_scale, x, timestep, model_options=model_options, cond=cond, uncond=uncond_), cfg_function(model, out[0], out[1], cond_scale_BIG, x, timestep, model_options=model_options, cond=cond, uncond=uncond_)


class CFGGuider_LanPaint:
    def outer_sample(self, noise, latent_image, sampler, sigmas, denoise_mask=None, callback=None, disable_pbar=False, seed=None):
        print("CFGGuider outer_sample")
        self.inner_model, self.conds, self.loaded_models = comfy.sampler_helpers.prepare_sampling(self.model_patcher, noise.shape, self.conds, self.model_options)
        device = self.model_patcher.load_device

        if denoise_mask is not None:
            denoise_mask = prepare_mask(denoise_mask, noise.shape, device)

        noise = noise.to(device)
        latent_image = latent_image.to(device)
        sigmas = sigmas.to(device)
        cast_to_load_options(self.model_options, device=device, dtype=self.model_patcher.model_dtype())

        try:
            self.model_patcher.pre_run()
            output = self.inner_sample(noise, latent_image, device, sampler, sigmas, denoise_mask, callback, disable_pbar, seed)
        finally:
            self.model_patcher.cleanup()

        comfy.sampler_helpers.cleanup_models(self.conds, self.loaded_models)
        del self.inner_model
        del self.loaded_models
        return output
    def predict_noise(self, x, timestep, model_options={}, seed=None):
        return sampling_function_LanPaint(self.inner_model, x, timestep, self.conds.get("negative", None), self.conds.get("positive", None), self.cfg, self.cfg_BIG, model_options=model_options, seed=seed)

#CFGGuider.outer_sample = CFGGuider_LanPaint.outer_sample
#CFGGuider.predict_noise = CFGGuider_LanPaint.predict_noise

class KSamplerX0Inpaint:
    def __init__(self, model, sigmas):
        self.inner_model = model
        self.sigmas = sigmas
        self.model_sigmas = torch.cat( (torch.tensor([0.], device = sigmas.device) , torch.tensor( self.inner_model.model_patcher.get_model_object("model_sampling").sigmas, device = sigmas.device) ) )
        self.model_sigmas = torch.tensor( self.model_sigmas, dtype = self.sigmas.dtype )
    def __call__(self, x, sigma, denoise_mask, model_options={}, seed=None,**kwargs):
        ### For 1.5 and XL model
        # x is x_t in the notation of variance exploding diffusion model, x_t = x_0 + sigma * noise
        # sigma is the noise level
        ### For flux model 
        # x is rectified flow x_t = sigma * noise + (1.0 - sigma) * x_0

        IS_FLUX = self.inner_model.inner_model.model_type == ModelType.FLUX
        IS_FLOW = self.inner_model.inner_model.model_type == ModelType.FLOW

        # unify the notations into variance exploding diffusion model
        if IS_FLUX or IS_FLOW:
            Flow_t = sigma
            abt = (1 - Flow_t)**2 / ((1 - Flow_t)**2 + Flow_t**2 )
            VE_Sigma = Flow_t / (1 - Flow_t)
            #print("t", torch.mean( sigma ).item(), "VE_Sigma", torch.mean( VE_Sigma ).item())
            

        else:
            VE_Sigma = sigma 
            abt = 1/( 1+VE_Sigma**2 )
            Flow_t = (1-abt)**0.5 / ( (1-abt)**0.5 + abt**0.5  )

        if denoise_mask is not None:
            if "denoise_mask_function" in model_options:
                denoise_mask = model_options["denoise_mask_function"](sigma, denoise_mask, extra_options={"model": self.inner_model, "sigmas": self.sigmas})

            denoise_mask = (denoise_mask > 0.5).float()

            latent_mask = 1 - denoise_mask
            current_times = (VE_Sigma, abt, Flow_t)

            current_step = torch.argmin( torch.abs( self.sigmas - torch.mean(sigma) ) )
            total_steps = len(self.sigmas)-1

            if total_steps - current_step <= self.LanPaint_early_stop:
                out = self.PaintMethod(x, self.latent_image, self.noise, sigma, latent_mask, current_times, model_options, seed, n_steps=0)
            else:
                out = self.PaintMethod(x, self.latent_image, self.noise, sigma, latent_mask, current_times, model_options, seed)
        else:
            out, _ = self.inner_model(x, sigma, model_options=model_options, seed=seed)
        
        # Add TAESD preview support - directly use the latent_preview module
        current_step = model_options.get("i", kwargs.get("i", 0))
        total_steps = model_options.get("total_steps", 0)

        # Only show preview every few steps to improve performance
        if current_step % 2 == 0:
            # Directly call the preview callback if it exists
            callback = model_options.get("callback", None)
            if callback is not None:
                callback({"i": current_step, "denoised": out, "x": x})
    
        return out

# Custom sampler class extending ComfyUI's KSAMPLER for LanPaint
class KSAMPLER(comfy.samplers.KSAMPLER):
    def sample(self, model_wrap, sigmas, extra_args, callback, noise, latent_image=None, denoise_mask=None, disable_pbar=False):
        #noise here is a randn noise from comfy.sample.prepare_noise
        #latent_image is the latent image as input of the KSampler node. For inpainting, it is the masked latent image. Otherwise it is zero tensor.
        extra_args["denoise_mask"] = denoise_mask
        model_k = KSamplerX0Inpaint(model_wrap, sigmas)
        model_k.latent_image = latent_image
        if self.inpaint_options.get("random", False): #TODO: Should this be the default?
            generator = torch.manual_seed(extra_args.get("seed", 41) + 1)
            model_k.noise = torch.randn(noise.shape, generator=generator, device="cpu").to(noise.dtype).to(noise.device)
        else:
            model_k.noise = noise

        IS_FLUX = model_wrap.inner_model.model_type == ModelType.FLUX
        IS_FLOW = model_wrap.inner_model.model_type == ModelType.FLOW
        # unify the notations into variance exploding diffusion model
        if IS_FLUX:
            model_wrap.cfg_BIG = 1.0
        else:
            model_wrap.cfg_BIG = model_wrap.model_patcher.LanPaint_cfg_BIG
        noise = model_wrap.inner_model.model_sampling.noise_scaling(sigmas[0], noise, latent_image, self.max_denoise(model_wrap, sigmas))

        model_k.PaintMethod = LanPaint(model_k.inner_model, 
                                       model_wrap.model_patcher.LanPaint_NumSteps,
                                       model_wrap.model_patcher.LanPaint_Friction,
                                       model_wrap.model_patcher.LanPaint_Lambda,
                                       model_wrap.model_patcher.LanPaint_Beta,
                                       model_wrap.model_patcher.LanPaint_StepSize, 
                                       IS_FLUX = IS_FLUX, 
                                       IS_FLOW = IS_FLOW)
        model_k.LanPaint_early_stop = model_wrap.model_patcher.LanPaint_EarlyStop
        #if not inpainting, after noise_scaling, noise = noise * sigma, which is the noise added to the clean latent image in the variance exploding diffusion model notation.
        #if inpainting, after noise_scaling, noise = latent_image + noise * sigma, which is x_t in the variance exploding diffusion model notation for the known region.
        k_callback = None
        total_steps = len(sigmas) - 1
        if callback is not None:
            k_callback = lambda x: callback(x["i"], x["denoised"], x["x"], total_steps)
        #print("LanPaint KSampler call sampler_function", self.sampler_function)
        # The main loop!
        #print("##########")
        #print("Sampling with ", self.sampler_function)
        #print("##########")
        samples = self.sampler_function(model_k, noise, sigmas, extra_args=extra_args, callback=k_callback, disable=disable_pbar, **self.extra_options)
        #print("LanPaint KSampler end sampler_function")
        samples = model_wrap.inner_model.model_sampling.inverse_noise_scaling(sigmas[-1], samples)
        return samples

@contextmanager
def override_sample_function():
    original_outer_sample = comfy.samplers.CFGGuider.outer_sample
    comfy.samplers.CFGGuider.outer_sample = CFGGuider_LanPaint.outer_sample

    original_predict_noise = comfy.samplers.CFGGuider.predict_noise
    comfy.samplers.CFGGuider.predict_noise = CFGGuider_LanPaint.predict_noise

    original_sample = comfy.samplers.KSAMPLER.sample
    comfy.samplers.KSAMPLER.sample = KSAMPLER.sample

    try:
        yield
    finally:
        comfy.samplers.KSAMPLER.sample = original_sample
        comfy.samplers.CFGGuider.predict_noise = original_predict_noise
        comfy.samplers.CFGGuider.outer_sample = original_outer_sample


class LanPaint_UpSale_LatentNoiseMask:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": { "samples": ("LATENT",),
                              "scale": ("INT", {"default": 2, "min": 2, "max": 8, "step": 1}),
                              }}
    RETURN_TYPES = ("LATENT",)
    FUNCTION = "set_mask"


    CATEGORY = "latent/inpaint"

    def set_mask(self, samples, scale):
        s = samples.copy()
        samples = s['samples']
        # generate a mask with every scaleth pixel set to 1
        mask = torch.zeros(samples.shape[0], 1, samples.shape[2], samples.shape[3], device=samples.device) + 1
        mask[:, :, ::scale, ::scale] = 0
        s["noise_mask"] = mask
        return (s,)

#KSAMPLER_NAMES = ["euler", "dpmpp_2m", "uni_pc"]
KSAMPLER_NAMES = ["euler","euler_ancestral", "heun", "heunpp2","dpm_2", "dpm_2_ancestral",
                "dpm_fast",  "dpmpp_sde", "dpmpp_sde_gpu",
                  "dpmpp_2m", "dpmpp_2m_sde", "dpmpp_2m_sde_gpu", "dpmpp_3m_sde", "dpmpp_3m_sde_gpu", "ddpm", 
                   "deis", "res_multistep", "res_multistep_ancestral", 
                  "gradient_estimation",  "er_sde", "seeds_2", "seeds_3"]

class LanPaint_KSampler():
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MODEL", {"tooltip": "The model used for denoising the input latent."}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff, "tooltip": "The random seed used for creating the noise."}),
                "steps": ("INT", {"default": 30, "min": 1, "max": 10000, "tooltip": "The number of steps used in the denoising process."}),
                "cfg": ("FLOAT", {"default": 5.0, "min": 0.0, "max": 100.0, "step":0.1, "round": 0.01, "tooltip": "The Classifier-Free Guidance scale balances creativity and adherence to the prompt. Higher values result in images more closely matching the prompt however too high values will negatively impact quality."}),
                "sampler_name": (KSAMPLER_NAMES, {"tooltip": "Recommended: euler."}),
                "scheduler": (comfy.samplers.KSampler.SCHEDULERS, {"default": "karras", "tooltip": "The scheduler controls how noise is gradually removed to form the image."}),
                "positive": ("CONDITIONING", {"tooltip": "The conditioning describing the attributes you want to include in the image."}),
                "negative": ("CONDITIONING", {"tooltip": "The conditioning describing the attributes you want to exclude from the image."}),
                "latent_image": ("LATENT", {"tooltip": "The latent image to denoise."}),
                "denoise": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "The amount of denoising applied, lower values will maintain the structure of the initial image allowing for image to image sampling."}),
                "LanPaint_NumSteps": ("INT", {"default": 5, "min": 0, "max": 100, "tooltip": "The number of steps for the Langevin dynamics, representing the turns of thinking per step."}),
                "LanPaint_PromptMode": (["Image First", "Prompt First"], {"tooltip": "Image First: emphasis image quality, Prompt First: emphasis prompt following"}),
                "LanPaint_Info": ("STRING", {"default": "LanPaint KSampler. For more info, visit https://github.com/scraed/LanPaint. If you find it useful, please give a star ⭐️!", "multiline": True}),
                  }
        }

    RETURN_TYPES = ("LATENT",)
    OUTPUT_TOOLTIPS = ("The denoised latent.",)
    FUNCTION = "sample"

    CATEGORY = "sampling"
    DESCRIPTION = "Uses the provided model, positive and negative conditioning to denoise the latent image."

    def sample(self, model, seed, steps, cfg, sampler_name, scheduler, positive, negative, latent_image, denoise=1.0, LanPaint_NumSteps=5, LanPaint_PromptMode = "Image First", LanPaint_Info=""):

        model.LanPaint_StepSize = 0.15
        model.LanPaint_Lambda = 16.0
        model.LanPaint_Beta = 1.
        model.LanPaint_NumSteps = LanPaint_NumSteps
        model.LanPaint_Friction = 15.
        model.LanPaint_EarlyStop = 1
        if LanPaint_PromptMode == "Image First":
            model.LanPaint_cfg_BIG = cfg
        else:
            model.LanPaint_cfg_BIG = 0*cfg - 0.5
        with override_sample_function():
            return nodes.common_ksampler(model, seed, steps, cfg, sampler_name, scheduler, positive, negative, latent_image, denoise=denoise)
class LanPaint_KSamplerAdvanced:
    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                    {"model": ("MODEL",),
                    "add_noise": (["enable", "disable"], ),
                    "noise_seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                    "steps": ("INT", {"default": 30, "min": 1, "max": 10000}),
                    "cfg": ("FLOAT", {"default": 5.0, "min": 0.0, "max": 100.0, "step":0.1, "round": 0.01}),
                    "sampler_name": (KSAMPLER_NAMES, ),
                    "scheduler": (comfy.samplers.KSampler.SCHEDULERS, ),
                    "positive": ("CONDITIONING", ),
                    "negative": ("CONDITIONING", ),
                    "latent_image": ("LATENT", ),
                    "start_at_step": ("INT", {"default": 0, "min": 0, "max": 10000}),
                    "end_at_step": ("INT", {"default": 10000, "min": 0, "max": 10000}),
                    "return_with_leftover_noise": (["disable", "enable"], ),
                "LanPaint_NumSteps": ("INT", {"default": 5, "min": 0, "max": 100, "tooltip": "The number of steps for the Langevin dynamics, representing the turns of thinking per step."}),
                "LanPaint_Lambda": ("FLOAT", {"default": 16., "min": 0.1, "max": 50.0, "step": 0.1, "round": 0.1, "tooltip": "The bidirectional guidance scale. Higher values align with known regions more closely, but may result in instability."}),
                "LanPaint_StepSize": ("FLOAT", {"default": 0.15, "min": 0.0001, "max": 1., "step": 0.01, "round": 0.001, "tooltip": "The step size for the Langevin dynamics. Higher values result in faster convergence but may be unstable."}),
                "LanPaint_Beta": ("FLOAT", {"default": 1., "min": 0.0001, "max": 5, "step": 0.1, "round": 0.1, "tooltip": "The step size ratio between masked / unmasked regions. Lower value can compensate high values of LanPaint_Lambda."}),
                "LanPaint_Friction": ("FLOAT", {"default": 15, "min": 0., "max": 50.0, "step": 0.1, "round": 0.1, "tooltip": "The friction parameter for fast langevin, lower values result in faster convergence but may be unstable."}),
                "LanPaint_PromptMode": (["Image First", "Prompt First"], {"tooltip": "Image First: emphasis image quality, Prompt First: emphasis prompt following"}),
                "LanPaint_EarlyStop": ("INT", {"default": 1, "min": 0, "max": 10000, "tooltip": "The number of steps to stop the LanPaint early, useful for preventing the image from irregular patterns."}),
                "LanPaint_Info": ("STRING", {"default": "LanPaint KSampler Adv. For more info, visit https://github.com/scraed/LanPaint. If you find it useful, please give a star ⭐️!", "multiline": True}),
                     },
                }

    RETURN_TYPES = ("LATENT",)
    FUNCTION = "sample"

    CATEGORY = "sampling"

    def sample(self, model, add_noise, noise_seed, steps, cfg, sampler_name, scheduler, positive, negative, latent_image, start_at_step, end_at_step, return_with_leftover_noise, denoise=1.0, LanPaint_StepSize=0.05, LanPaint_Lambda=5, LanPaint_Beta=1, LanPaint_NumSteps=5, LanPaint_Friction=5, LanPaint_PromptMode = "Image First", LanPaint_EarlyStop = 1, LanPaint_Info=""):
        force_full_denoise = True
        if return_with_leftover_noise == "enable":
            force_full_denoise = False
        disable_noise = False
        if add_noise == "disable":
            disable_noise = True
        model.LanPaint_StepSize = LanPaint_StepSize
        model.LanPaint_Lambda = LanPaint_Lambda
        model.LanPaint_Beta = LanPaint_Beta
        model.LanPaint_NumSteps = LanPaint_NumSteps
        model.LanPaint_Friction = LanPaint_Friction
        model.LanPaint_EarlyStop = LanPaint_EarlyStop
        if LanPaint_PromptMode == "Image First":
            model.LanPaint_cfg_BIG = cfg
        else:
            model.LanPaint_cfg_BIG = 0*cfg - 0.5

        with override_sample_function():
            return nodes.common_ksampler(model, noise_seed, steps, cfg, sampler_name, scheduler, positive, negative, latent_image, denoise=denoise, disable_noise=disable_noise, start_step=start_at_step, last_step=end_at_step, force_full_denoise=force_full_denoise)


class MaskBlend:
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "image1": ("IMAGE", {"tooltip": "Image before inpaint"}),
                "image2": ("IMAGE", {"tooltip": "Image after inpaint"}),
                "mask": ("MASK",),
                "blend_overlap": ("INT", {"default": 1, "min": 1, "max": 51, "step": 2, "tooltip": "The number of pixels to blend between the two images."})
            },
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "blend_images"

    CATEGORY = "image/postprocessing"

    def blend_images(self, image1: torch.Tensor, image2: torch.Tensor, mask: torch.Tensor, blend_overlap: int):
        # smooth the binary 01 mask, keep 1 still 1, but smooth the transition from 1 to 0
        # for each mask pixel, find out the nearest 1 pixel, and set the mask value to the distance between the two pixels
        # check the size of mask and image1, image2, if not the same, assert error
        if image1.shape[1] != image2.shape[1] or image1.shape[2] != image2.shape[2]:
            raise ValueError("Make sure your image size is a multiple of 8. Otherwise the mask will not be aligned with the output image.")
        
        mask = mask.float()
        mask = torch.nn.functional.max_pool2d(mask, kernel_size=blend_overlap, stride=1, padding=blend_overlap//2)
        # apply Gaussian blur with kernel size blend_overlap
        kernel = self.gaussian_kernel(blend_overlap)
        kernel = kernel.to(image1.device)
        kernel = kernel[None, None, ...]
        
        mask = torch.nn.functional.conv2d(mask[:,None,:,:], kernel, padding=blend_overlap//2)[:,0,:,:]


        blended_image = image1 * (1 - mask[...,None]) + image2 * mask[...,None]
        return (blended_image,)
    def gaussian_kernel(self,kernel_size):
        """
        Creates a 2D Gaussian kernel with the given size and standard deviation (sigma).
        """
        sigma = (kernel_size - 1)/4
        # Create a grid of (x, y) coordinates
        x = torch.arange(kernel_size).float() - kernel_size // 2
        y = torch.arange(kernel_size).float() - kernel_size // 2
        x_grid, y_grid = torch.meshgrid(x, y, indexing='ij')

        # Compute the Gaussian function
        kernel = torch.exp(-(x_grid ** 2 + y_grid ** 2) / (2 * sigma ** 2))
        kernel = kernel / kernel.sum()  # Normalize the kernel

        return kernel


# A dictionary that contains all nodes you want to export with their names
# NOTE: names should be globally unique
NODE_CLASS_MAPPINGS = {
    "LanPaint_KSampler": LanPaint_KSampler,
    "LanPaint_KSamplerAdvanced": LanPaint_KSamplerAdvanced,
    "LanPaint_MaskBlend": MaskBlend,
#    "LanPaint_UpSale_LatentNoiseMask": LanPaint_UpSale_LatentNoiseMask,
}

# A dictionary that contains the friendly/humanly readable titles for the nodes
NODE_DISPLAY_NAME_MAPPINGS = {
    "LanPaint_KSampler": "LanPaint KSampler",
    "LanPaint_KSamplerAdvanced": "LanPaint KSampler (Advanced)",
    "LanPaint_MaskBlend": "LanPaint Mask Blend",
#    "LanPaint_UpSale_LatentNoiseMask": "LanPaint UpSale Latent Noise Mask"
}
