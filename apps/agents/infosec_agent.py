import os
import json
import asyncio
from typing import List

from camel.loaders import LocalMinerUReader
from camel.embeddings import OpenAIEmbedding, BaseEmbedding, SentenceTransformerEncoder
from camel.storages import QdrantStorage
from camel.retrievers import VectorRetriever
from camel.agents import ChatAgent
from camel.messages import BaseMessage
from camel.types import ModelType, RoleType, ModelPlatformType
from camel.models import ModelFactory
from camel.configs import ChatGPTConfig

async def main():
    # 1. Configuration
    # We assume the user has a local OpenAI-compatible server running (e.g. vLLM or Ollama)
    # serving the 'qwen3-vl:8b' model.
    
    # Check for API Key, otherwise use local SentenceTransformer
    openai_api_key = os.environ.get("OPENAI_API_KEY")
    if not openai_api_key:
        print("OPENAI_API_KEY not set. Using local SentenceTransformer for embeddings.")
        try:
            embedding_model = SentenceTransformerEncoder(model_name="intfloat/e5-small-v2") # Smaller model for speed
        except ImportError:
            print("sentence_transformers not installed. Please install it with `pip install sentence-transformers`.")
            return
    else:
        embedding_model = OpenAIEmbedding()

    # 2. Document Processing with MinerU
    file_path = r"C:\SynologyDrive\INFOAGEN\source\docs\cli.pdf"
    print(f"Processing {file_path} with MinerU...")
    
    reader = LocalMinerUReader()
    # Updated output directory as requested
    reader.output_dir = r"C:\SynologyDrive\INFOAGEN\INAGENT\doc_local"
    os.makedirs(reader.output_dir, exist_ok=True)

    try:
        # Submit task (async)
        task_id = await reader.submit_task(file_path)
        print(f"Task submitted, ID: {task_id}")
        
        # Retrieve output
        json_output = await reader.get_task_output(task_id)
        print("Successfully retrieved MinerU structured output.")
        
        # Parse content list
        content_list = json.loads(json_output)
        
    except Exception as e:
        print(f"Error processing file: {e}")
        return

    # 3. Vector Storage & Indexing
    print("Indexing content into Vector Storage...")
    vector_storage = QdrantStorage(
        vector_dim=embedding_model.get_output_dim(),
        collection_name="cli_docs_infosec",
        path=r"C:\SynologyDrive\INFOAGEN\INAGENT\local_data\qdrant_infosec"
    )

    # Prepare chunks
    # MinerU outputs structured blocks. We can treat each block (or group) as a chunk.
    texts = []
    metadatas = []
    
    for block in content_list:
        text = block.get("text", "").strip()
        # Filter out very short text (likely noise)
        if text and len(text) >= 10:
            texts.append(text)
            metadatas.append({
                "type": block.get("type"),
                "bbox": str(block.get("bbox")),
                "page_idx": block.get("page_idx"),
                "text_level": block.get("text_level")
            })

    if texts:
        # Add to storage (this automatically embeds)
        # Note: VectorRetriever.process usually handles file reading and chunking via Unstructured.
        # Here we manually add because we already have structured data from MinerU.
        # We need to use the storage directly or use a custom method.
        # VectorRetriever doesn't have a direct 'add_texts' method in some versions, 
        # but QdrantStorage does have 'add'.
        
        # Let's see if we can use VectorRetriever to manage this.
        # VectorRetriever initializes with storage.
        # We can manually embed and add to storage.
        
        embeddings = embedding_model.embed_list(texts)
        records = []
        from camel.storages import VectorRecord
        for i, (text, embed, meta) in enumerate(zip(texts, embeddings, metadatas)):
            records.append(VectorRecord(vector=embed, payload={"text": text, **meta}))
        
        vector_storage.add(records)
        print(f"Indexed {len(records)} chunks.")
    else:
        print("No valid text found to index.")
        return

    # 4. Initialize Retriever
    retriever = VectorRetriever(embedding_model=embedding_model, storage=vector_storage)

    # 5. Initialize Infosec Agent (CLI Expert)
    # Configuration for Local Model (qwen3-vl:8b)
    # Assuming vLLM running on localhost:8000
    
    model_name = "qwen3-vl:8b"
    server_url = "http://localhost:8000/v1"
    
    print(f"Initializing Infosec Agent with model {model_name} at {server_url}...")
    
    try:
        # Create model using OpenAI compatible platform
        model = ModelFactory.create(
            model_platform=ModelPlatformType.OPENAI,
            model_type=model_name,
            model_config_dict=ChatGPTConfig(temperature=0.0).as_dict(),
            url=server_url,
            api_key="EMPTY" # vLLM usually accepts any key or "EMPTY"
        )
        
        sys_msg_content = """You are Infosec Agent, an expert CLI documentation assistant.
Your goal is to help users retrieve configuration details and translate them into understandable explanations or other languages if requested.
Use the provided context from the CLI documentation to answer questions accurately.
If the context contains specific commands, format them clearly.
"""
        sys_msg = BaseMessage.make_assistant_message(
            role_name="Infosec Agent",
            content=sys_msg_content
        )
        
        agent = ChatAgent(system_message=sys_msg, model=model)
        
    except Exception as e:
        print(f"Failed to initialize local model: {e}")
        print("Falling back to GPT-3.5-Turbo for demonstration (requires OPENAI_API_KEY).")
        agent = ChatAgent(
            system_message=BaseMessage.make_assistant_message(
                role_name="Infosec Agent",
                content="You are Infosec Agent, a CLI expert."
            ),
            model=ModelType.GPT_3_5_TURBO
        )

    # 6. Interaction Loop
    print("\nInfosec Agent is ready! (Type 'exit' to quit)")
    
    while True:
        user_input = input("\nUser: ")
        if user_input.lower() in ["exit", "quit"]:
            break
            
        # Retrieve context
        try:
            retrieved_info = retriever.query(user_input, top_k=3)
            # Extact text from retrieved info
            # retrieved_info is typically a list of dicts with 'text' key? 
            # Or the query method returns a string in some versions?
            # Based on the cookbook: "returned dictionary list includes... text"
            # But earlier in my code I saw 'retrieved_info' being printed.
            
            # Let's handle the structure.
            context_segments = []
            if isinstance(retrieved_info, list):
                for item in retrieved_info:
                    # Check structure
                    if isinstance(item, dict):
                        content = item.get("text") or item.get("content")
                        if content:
                            context_segments.append(content)
                    elif isinstance(item, str):
                        context_segments.append(item)
            elif isinstance(retrieved_info, str):
                context_segments.append(retrieved_info)
                
            context_str = "\n---\n".join(context_segments)
            
        except Exception as e:
            print(f"Retrieval failed: {e}")
            context_str = "No context retrieved due to error."

        # Construct message
        prompt = f"""Context from CLI Documentation:
{context_str}

User Question: {user_input}

Please answer the question based on the context. If you are translating configuration, please provide the original command and the translation."""

        user_msg = BaseMessage.make_user_message(role_name="User", content=prompt)
        
        try:
            response = agent.step(user_msg)
            print(f"\nInfosec Agent: {response.msg.content}")
        except Exception as e:
            print(f"Agent failed to respond: {e}")

if __name__ == "__main__":
    asyncio.run(main())
