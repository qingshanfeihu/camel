#!/usr/bin/env python
# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========

"""Launcher script for LLM Gateway - loads .env and starts server."""

import os
import sys
import logging
from pathlib import Path
from datetime import datetime
import io

# Force UTF-8 encoding for stdout/stderr on Windows with error handling
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, 
        encoding='utf-8', 
        errors='replace',
        line_buffering=True
    )
    sys.stderr = io.TextIOWrapper(
        sys.stderr.buffer, 
        encoding='utf-8', 
        errors='replace',
        line_buffering=True
    )

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def setup_logging():
    """Configure logging to both file and console."""
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    
    # Create log file with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"gateway_{timestamp}.log"
    
    # Configure root logger with safe encoding
    class SafeStreamHandler(logging.StreamHandler):
        def emit(self, record):
            try:
                msg = self.format(record)
                stream = self.stream
                # Use errors='replace' for safe output
                stream.write(msg + self.terminator)
                self.flush()
            except Exception:
                self.handleError(record)
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8', errors='replace'),
            SafeStreamHandler(sys.stdout)
        ]
    )
    
    logger = logging.getLogger(__name__)
    logger.info(f"Logging initialized - log file: {log_file}")
    return logger


def load_env_file(env_path: str, logger):
    """Load environment variables from .env file."""
    if not os.path.exists(env_path):
        logger.warning(f"Warning: {env_path} not found")
        return
    
    logger.info(f"Loading environment from {env_path}...")
    try:
        with open(env_path, 'r', encoding='utf-8', errors='ignore') as f:
            count = 0
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                
                if '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    os.environ[key] = value
                    count += 1
        
        logger.info(f"Environment loaded successfully ({count} variables)")
    except Exception as e:
        logger.error(f"Error loading environment: {e}")


def main():
    """Main function."""
    # Setup logging first
    logger = setup_logging()
    
    # Get current directory
    current_dir = Path(__file__).parent.parent
    
    # Load environment from INAGENT/.env
    env_path = current_dir / "INAGENT" / ".env"
    load_env_file(str(env_path), logger)
    
    # Set gateway config
    config_path = current_dir / "llm_gateway" / "config.yaml"
    os.environ["GATEWAY_CONFIG"] = str(config_path)
    
    # Set port
    port = int(os.getenv("GATEWAY_PORT", "9000"))
    
    logger.info("=" * 80)
    logger.info("INFOAGEN LLM Gateway")
    logger.info("=" * 80)
    logger.info(f"Config: {config_path}")
    logger.info(f"Port: {port}")
    logger.info("Upstream base URL: configured in gateway config")
    logger.info("=" * 80)
    
    # Import and run uvicorn
    import uvicorn
    
    uvicorn.run(
        "llm_gateway.gateway:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        reload=False
    )


if __name__ == "__main__":
    main()
