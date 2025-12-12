import os
import argparse
import io
import pickle
import numpy as np
import pandas as pd
import torch
import openslide
import boto3
from PIL import Image
from tqdm import tqdm
from transformers import AutoModel, AutoImageProcessor

# Configuration defaults
DEFAULT_PATCH_SIZE = 512  # Size at the specified level (usually level 0 for 20x, or equivalent)
DEFAULT_TARGET_SIZE = 224 # Input size for CONCH
BATCH_SIZE = 32

def get_s3_client():
    """Initialize S3 client."""
    return boto3.client('s3')

def check_s3_exists(s3_client, bucket, key):
    """Check if a file exists in S3."""
    try:
        s3_client.head_object(Bucket=bucket, Key=key)
        return True
    except:
        return False

def save_to_s3(s3_client, bucket, key, data):
    """Save data dictionary to S3 as a pickle file."""
    buffer = io.BytesIO()
    pickle.dump(data, buffer)
    buffer.seek(0)
    s3_client.upload_fileobj(buffer, bucket, key)

def is_tissue(patch, threshold=235):
    """Simple tissue detection based on mean intensity.
    Assumes white background. Returns True if tissue is present."""
    # Convert to grayscale
    gray = patch.convert('L')
    # Calculate mean intensity
    mean_intensity = np.mean(np.array(gray))
    # If mean intensity is less than threshold, it's likely tissue (darker than white background)
    return mean_intensity < threshold

def extract_and_encode_patches(slide_path, patch_size, target_size, processor, model, device):
    """
    Extracts patches from the slide, preprocesses them, and extracts features using CONCH.
    Returns:
        features (torch.Tensor): (N, D) patch features
        coords (torch.Tensor): (N, 2) coordinates (x, y) at level 0
    """
    slide = openslide.OpenSlide(slide_path)
    
    # Generate grid coordinates (Level 0)
    w, h = slide.dimensions
    
    # We assume we process at level 0 for simplicity, or adjust stride if using other levels
    # TITAN expects coordinates at level 0.
    
    coordinates = []
    patches = []
    
    # Iterate over the slide grid
    for y in range(0, h, patch_size):
        for x in range(0, w, patch_size):
            # Check if patch is within bounds
            if x + patch_size > w or y + patch_size > h:
                continue
                
            # Read region (Level 0)
            # Note: Reading one by one is slow. For production, use a generator/dataloader approach.
            # Here we read, check tissue, and if valid, add to list.
            # To save memory, we might process in batches.
            
            # Read low-res thumbnail for quick tissue check could be an optimization,
            # but here we do per-patch read for simplicity/correctness.
            patch = slide.read_region((x, y), 0, (patch_size, patch_size)).convert('RGB')
            
            if is_tissue(patch):
                coordinates.append([x, y])
                patches.append(patch.resize((target_size, target_size)))

    if not patches:
        return None, None

    # Process patches in batches to avoid OOM
    all_features = []
    
    # CONCH encoding
    model.eval()
    with torch.no_grad():
        for i in range(0, len(patches), BATCH_SIZE):
            batch_patches = patches[i : i + BATCH_SIZE]
            
            # Preprocess
            inputs = processor(images=batch_patches, return_tensors="pt").to(device)
            
            # Forward pass (get image embeddings)
            outputs = model.get_image_features(**inputs) # Adjust method based on CONCH API
            # Note: CONCH v1.5 might use standard CLIP-like API or specific method.
            # Assuming CLIP-like 'get_image_features' or typical forward output.
            # If it's a standard HF model, outputs.image_embeds or outputs[0]
            
            # Check outputs type
            if hasattr(outputs, 'image_embeds'):
                feats = outputs.image_embeds
            else:
                feats = outputs # Fallback
                
            all_features.append(feats.cpu())
            
    features = torch.cat(all_features, dim=0)
    coords = torch.tensor(coordinates)
    
    slide.close()
    return features, coords

def main():
    parser = argparse.ArgumentParser(description="Inference with TITAN on WSI")
    parser.add_argument("--csv_path", type=str, required=True, help="Path to the CSV file")
    parser.add_argument("--data_root", type=str, required=True, help="Parent directory of the dataset")
    parser.add_argument("--bucket", type=str, required=True, help="S3 Bucket name")
    parser.add_argument("--prefix", type=str, default="titan_inference", help="S3 Prefix for output")
    parser.add_argument("--patch_size", type=int, default=DEFAULT_PATCH_SIZE, help="Patch size at Level 0")
    
    parser.add_argument("--hf_token", type=str, default=None, help="Optional: Hugging Face token for authentication")
    
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Using device: {device}")

    # 1. Load Models
    print("Loading CONCH...")
    try:
        conch_processor = AutoImageProcessor.from_pretrained("MahmoodLab/CONCH", trust_remote_code=True)
        conch_model = AutoModel.from_pretrained("MahmoodLab/CONCH", trust_remote_code=True).to(device)
    except Exception as e:
        print(f"Error loading CONCH: {e}")
        print("Please ensure you have access and are logged into Hugging Face.")
        return

    print("Loading TITAN...")
    try:
        titan_model = AutoModel.from_pretrained("MahmoodLab/TITAN", trust_remote_code=True, token=args.hf_token).to(device)
        titan_model.eval()
    except Exception as e:
        print(f"Error loading TITAN: {e}")
        return

    # 2. Read CSV
    df = pd.read_csv(args.csv_path)
    print(f"Found {len(df)} slides to process.")

    s3 = get_s3_client()

    # 3. Iterate and Process
    for idx, row in tqdm(df.iterrows(), total=len(df)):
        folder = row['FOLDER']
        filename = row['FILE_NAME']
        
        # Construct paths
        file_path = os.path.join(args.data_root, folder, filename)
        s3_key = f"{args.prefix}/{filename.replace('.svs', '.pkl')}"
        
        # Check existence
        if check_s3_exists(s3, args.bucket, s3_key):
            # print(f"Skipping {filename}, already exists in S3.")
            continue
            
        if not os.path.exists(file_path):
            print(f"File not found: {file_path}, skipping.")
            continue
            
        try:
            # Step 1: Extract Patches and Features (CONCH)
            # Note: This can be time-consuming.
            features, coords = extract_and_encode_patches(
                file_path, 
                args.patch_size, 
                DEFAULT_TARGET_SIZE, 
                conch_processor, 
                conch_model, 
                device
            )
            
            if features is None or len(features) == 0:
                print(f"No tissue found in {filename}, skipping.")
                continue

            # Step 2: TITAN Inference
            with torch.no_grad():
                # Prepare inputs for TITAN
                # encode_slide_from_patch_features(features, coords, patch_size_lv0)
                # features: (N, D)
                # coords: (N, 2)
                # patch_size_lv0: int tensor
                
                patch_size_tensor = torch.tensor(args.patch_size)
                
                # TITAN expects inputs on device
                features_dev = features.to(device)
                coords_dev = coords.to(device)
                patch_size_dev = patch_size_tensor.to(device)
                
                # Get Slide Embedding
                slide_embedding = titan_model.encode_slide_from_patch_features(
                    features_dev, 
                    coords_dev, 
                    patch_size_dev
                )
                
                # Note: We are not extracting attention weights here as the API 
                # 'encode_slide_from_patch_features' typically returns just the embedding.
                # However, we save 'features' and 'coords' which are required to reconstruction
                # heatmaps or run downstream attention-based visualization.
            
            # Prepare result payload
            result = {
                'file_name': filename,
                'slide_embedding': slide_embedding.cpu().numpy(),
                'features': features.cpu().numpy(), # Saved for heatmap generation
                'coords': coords.cpu().numpy(),     # Saved for heatmap generation
                'patch_size': args.patch_size
            }
            
            # Step 3: Save to S3
            save_to_s3(s3, args.bucket, s3_key, result)
            
        except Exception as e:
            print(f"Error processing {filename}: {e}")
            continue

    print("Processing complete.")

if __name__ == "__main__":
    main()
