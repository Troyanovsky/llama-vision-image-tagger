from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import os
from pathlib import Path
from typing import List, Dict, Set, Optional
import json
import logging
from image_processor import ImageProcessor, update_image_metadata
from vector_store import VectorStore

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_name: str = "llama3.2-vision"


settings = Settings()

app = FastAPI()

# Mount static files (your frontend)
app.mount("/static", StaticFiles(directory="static"), name="static")

# We don't need CORS middleware anymore since frontend and backend are served from same origin
# app.add_middleware(CORSMiddleware, ...)

# Set up basic logging configuration
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class FolderRequest(BaseModel):
    folder_path: str

class ImageInfo(BaseModel):
    name: str
    path: str
    description: str = ""
    tags: List[str] = []
    text_content: str = ""
    is_processed: bool = False

class SearchRequest(BaseModel):
    query: str

class ProcessImageRequest(BaseModel):
    image_path: str

class UpdateImageMetadata(BaseModel):
    path: str
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    text_content: Optional[str] = None

def get_supported_extensions() -> Set[str]:
    """Return a set of supported image file extensions."""
    return {'.jpg', '.jpeg', '.png', '.webp'}

def initialize_image_metadata(image_path: str) -> Dict:
    """Create initial metadata structure for a single image."""
    return {
        "description": "",
        "tags": [],
        "text_content": "",
        "is_processed": False
    }

def is_metadata_processed(metadata: Dict) -> bool:
    """Check if any metadata field is non-empty."""
    return bool(
        metadata.get("description") or 
        metadata.get("tags") or 
        metadata.get("text_content")
    )

def scan_folder_for_images(folder_path: Path) -> Dict[str, Dict]:
    """Scan folder recursively and create metadata for all images."""
    metadata = {}
    image_extensions = get_supported_extensions()
    
    for file_path in folder_path.rglob("*"):
        if file_path.suffix.lower() in image_extensions:
            rel_path = str(file_path.relative_to(folder_path))
            metadata[rel_path] = initialize_image_metadata(rel_path)
    
    return metadata

def load_or_create_metadata(folder_path: Path) -> Dict[str, Dict]:
    """Load existing metadata file or create new one if it doesn't exist.
    Update metadata by adding new images and removing old records."""
    metadata_file = folder_path / "image_metadata.json"
    image_extensions = get_supported_extensions()
    
    # Load existing metadata if it exists
    if metadata_file.exists():
        with open(metadata_file, 'r') as f:
            metadata = json.load(f)
    else:
        metadata = {}

    # Scan folder for current images
    current_images = {str(file_path.relative_to(folder_path)): file_path
                      for file_path in folder_path.rglob("*")
                      if file_path.suffix.lower() in image_extensions}

    # Add new images to metadata
    for rel_path in current_images:
        if rel_path not in metadata:
            metadata[rel_path] = initialize_image_metadata(rel_path)
        # Update is_processed based on metadata content
        metadata[rel_path]["is_processed"] = is_metadata_processed(metadata[rel_path])

    # Remove old records from metadata
    for rel_path in list(metadata.keys()):
        if rel_path not in current_images:
            del metadata[rel_path]

    # Save updated metadata
    with open(metadata_file, 'w') as f:
        json.dump(metadata, f, indent=4)

    # Sync with vector store using the instance-specific vector store
    app.vector_store.sync_with_metadata(folder_path, metadata)

    return metadata

def create_image_info(rel_path: str, metadata: Dict) -> ImageInfo:
    """Create ImageInfo object from metadata."""
    info = metadata.get(rel_path, {})
    return ImageInfo(
        name=Path(rel_path).name,
        path=rel_path,
        description=info.get("description", ""),
        tags=info.get("tags", []),
        text_content=info.get("text_content", ""),
        is_processed=info.get("is_processed", False)
    )

def search_images(query: str, metadata: Dict[str, Dict]) -> List[Dict]:
    """
    Hybrid search combining full-text and vector search.
    Returns list of matching images with their metadata.
    """
    results = set()
    
    # Full-text search
    if query:
        query = query.lower()
        for path, meta in metadata.items():
            # Check if query matches any of the text fields
            if (query in meta.get("description", "").lower() or
                query in meta.get("text_content", "").lower() or
                any(query in tag.lower() for tag in meta.get("tags", []))):
                
                results.add(path)
    else:
        # If no query, return all images
        results.update(metadata.keys())
    
    # Vector search
    vector_results = app.vector_store.search_images(query)
    results.update(vector_results)
    
    # Convert results to list of dicts with metadata
    search_results = []
    for path in results:
        if path in metadata:  # Ensure the path exists in metadata
            search_results.append({
                "name": Path(path).name,
                "path": path,
                **metadata[path]
            })
    
    return search_results

@app.get("/")
async def read_root():
    return FileResponse("static/index.html")

@app.post("/images")
async def get_images(request: FolderRequest):
    folder_path = Path(request.folder_path)
    
    logger.info(f"Received request to open folder: {folder_path}")
    
    if not folder_path.exists() or not folder_path.is_dir():
        logger.error(f"Folder not found: {folder_path}")
        raise HTTPException(status_code=404, detail="Folder not found")
    
    app.current_folder = str(folder_path)
    
    try:
        # Initialize vector store in the selected folder
        vector_store_path = folder_path / ".vectordb"
        app.vector_store = VectorStore(persist_directory=str(vector_store_path))
        
        metadata = load_or_create_metadata(folder_path)
        images = [create_image_info(rel_path, metadata) 
                  for rel_path in metadata.keys()]
        logger.info(f"Successfully processed folder: {folder_path}")
        return {"images": images}
    except Exception as e:
        logger.error(f"Error processing folder {folder_path}: {str(e)}")
        raise HTTPException(status_code=500, 
                            detail=f"Error processing folder: {str(e)}")

@app.get("/image/{path:path}")
async def get_image(path: str, request: FolderRequest = None):
    try:
        # Get the folder path from the most recent request
        # Note: This is a simplified solution. In a production environment,
        # you might want to store this in a session or database
        if not hasattr(app, 'current_folder'):
            raise HTTPException(status_code=400, detail="No folder selected")
        
        # Combine the base folder path with the relative image path
        full_path = os.path.join(app.current_folder, path)
        
        if not os.path.exists(full_path):
            raise HTTPException(status_code=404, detail="Image not found")
        
        return FileResponse(full_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/search")
async def search_endpoint(request: SearchRequest):
    """
    Search images using hybrid search (full-text + vector).
    """
    if not hasattr(app, 'current_folder') or not hasattr(app, 'vector_store'):
        raise HTTPException(status_code=400, detail="No folder selected")
    
    try:
        folder_path = Path(app.current_folder)
        
        # Load current metadata
        metadata_file = folder_path / "image_metadata.json"
        if not metadata_file.exists():
            raise HTTPException(status_code=400, detail="No metadata file found")
            
        with open(metadata_file, 'r') as f:
            metadata = json.load(f)
        
        # Use the instance-specific vector store
        matching_images = search_images(request.query, metadata)
        
        # Convert to ImageInfo objects
        images = [ImageInfo(
            name=img["name"],
            path=img["path"],
            description=img.get("description", ""),
            tags=img.get("tags", []),
            text_content=img.get("text_content", ""),
            is_processed=img.get("is_processed", False)
        ) for img in matching_images]
        
        return {"images": images}
    
    except Exception as e:
        logger.error(f"Error searching images: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error searching images: {str(e)}")

@app.post("/process-image")
async def process_image(request: ProcessImageRequest):
    """
    Process an image using Ollama to generate tags, description, and extract text.
    """
    if not hasattr(app, 'current_folder') or not hasattr(app, 'vector_store'):
        raise HTTPException(status_code=400, detail="No folder selected")

    try:
        folder_path = Path(app.current_folder)
        full_image_path = folder_path / request.image_path

        if not full_image_path.exists():
            raise HTTPException(status_code=404, detail="Image not found")

        # Process the image
        processor = ImageProcessor(settings.model_name)
        metadata = await processor.process_image(full_image_path)

        # Update metadata file
        update_image_metadata(folder_path, request.image_path, metadata)
        
        # Update vector store
        app.vector_store.add_or_update_image(request.image_path, metadata)

        return {
            "path": request.image_path,
            "url": f"/image/{request.image_path}",
            **metadata
        }

    except Exception as e:
        logger.error(f"Error processing image: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error processing image: {str(e)}")

@app.post("/update-metadata")
async def update_metadata(request: UpdateImageMetadata):
    """Update metadata for a specific image."""
    if not hasattr(app, 'current_folder') or not hasattr(app, 'vector_store'):
        raise HTTPException(status_code=400, detail="No folder selected")
    
    try:
        folder_path = Path(app.current_folder)
        metadata_updates = {
            "description": request.description,
            "tags": request.tags,
            "text_content": request.text_content,
            "is_processed": bool(
                request.description or 
                request.tags or 
                request.text_content
            )
        }
        
        # Update the metadata file
        update_image_metadata(folder_path, request.path, metadata_updates)
        
        # Update vector store using the instance-specific vector store
        app.vector_store.add_or_update_image(request.path, metadata_updates)
        
        return {"status": "success"}
        
    except Exception as e:
        logger.error(f"Error updating metadata: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error updating metadata: {str(e)}")

@app.get("/check-init-status")
async def check_init_status():
    """Check if this is the first time initialization."""
    try:
        if not hasattr(app, 'current_folder'):
            raise HTTPException(status_code=400, detail="No folder selected")
            
        # Check if the vector store directory exists in the selected folder
        folder_path = Path(app.current_folder)
        vector_store_path = folder_path / ".vectordb"
        needs_init = not vector_store_path.exists()
        return {"needs_init": needs_init}
    except Exception as e:
        logger.error(f"Error checking init status: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000) 
    # or in command line: uvicorn main:app --host 127.0.0.1 --port 8000 --reload
