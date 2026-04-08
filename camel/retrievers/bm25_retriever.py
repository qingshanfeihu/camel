# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
import re
from typing import Any, Dict, List

from camel.loaders import UnstructuredIO
from camel.retrievers import BaseRetriever
from camel.utils import dependencies_required

DEFAULT_TOP_K_RESULTS = 1

_CJK_RE = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]')
_NON_ALNUM_RE = re.compile(r'[^\w\s]', re.UNICODE)
_META_JSON_RE = re.compile(r'^INAGENT_META_JSON:\{[^}]*\}\n?')


def _tokenize(text: str) -> List[str]:
    r"""CJK-aware tokenizer: bigram + unigram for CJK, whitespace split for
    Latin/digit tokens. Falls back to plain ``str.split(" ")`` when no CJK
    characters are detected so that existing non-CJK workloads are
    unaffected.
    """
    text = _META_JSON_RE.sub('', text)
    if not _CJK_RE.search(text):
        return text.split(" ")

    text = _NON_ALNUM_RE.sub(' ', text).lower()
    tokens: List[str] = []
    buf: List[str] = []

    def _flush_latin():
        if buf:
            word = ''.join(buf).strip()
            if word:
                tokens.append(word)
            buf.clear()

    for ch in text:
        if '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf':
            _flush_latin()
            tokens.append(ch)
        elif ch in (' ', '\t', '\n', '\r'):
            _flush_latin()
        else:
            buf.append(ch)
    _flush_latin()

    bigrams: List[str] = []
    for i in range(len(tokens) - 1):
        if len(tokens[i]) == 1 and len(tokens[i + 1]) == 1:
            if (_CJK_RE.match(tokens[i]) and _CJK_RE.match(tokens[i + 1])):
                bigrams.append(tokens[i] + tokens[i + 1])
    tokens.extend(bigrams)
    return tokens


class BM25Retriever(BaseRetriever):
    r"""An implementation of the `BaseRetriever` using the `BM25` model.

    This class facilitates the retriever of relevant information using a
    query-based approach, it ranks documents based on the occurrence and
    frequency of the query terms.

    Attributes:
        bm25 (BM25Okapi): An instance of the BM25Okapi class used for
            calculating document scores.
        content_input_path (str): The path to the content that has been
            processed and stored.
        unstructured_modules (UnstructuredIO): A module for parsing files and
            URLs and chunking content based on specified parameters.

    References:
        https://github.com/dorianbrown/rank_bm25
    """

    @dependencies_required('rank_bm25')
    def __init__(self) -> None:
        r"""Initializes the BM25Retriever."""
        from rank_bm25 import BM25Okapi

        self.bm25: BM25Okapi = None
        self.content_input_path: str = ""
        self.unstructured_modules: UnstructuredIO = UnstructuredIO()

    def process(
        self,
        content_input_path: str,
        chunk_type: str = "chunk_by_title",
        **kwargs: Any,
    ) -> None:
        r"""Processes content from a file or URL, divides it into chunks by
        using `Unstructured IO`,then stored internally. This method must be
        called before executing queries with the retriever.

        Args:
            content_input_path (str): File path or URL of the content to be
                processed.
            chunk_type (str): Type of chunking going to apply. Defaults to
                "chunk_by_title".
            **kwargs (Any): Additional keyword arguments for content parsing.
        """
        from rank_bm25 import BM25Okapi
        from unstructured.documents.elements import Element

        # Load and preprocess documents
        self.content_input_path = str(content_input_path)
        
        if isinstance(content_input_path, list) and all(isinstance(e, Element) for e in content_input_path):
            elements = content_input_path
            self.chunks = elements
        else:
            elements = self.unstructured_modules.parse_file_or_url(
                content_input_path, **kwargs
            )
            if elements:
                self.chunks = self.unstructured_modules.chunk_elements(
                    chunk_type=chunk_type, elements=elements
                )
            else:
                self.chunks = []

        if self.chunks:

            # Convert chunks to a list of strings for tokenization
            tokenized_corpus = [_tokenize(str(chunk)) for chunk in self.chunks]
            self.bm25 = BM25Okapi(tokenized_corpus)
        else:
            self.bm25 = None

    def query(
        self,
        query: str,
        top_k: int = DEFAULT_TOP_K_RESULTS,
    ) -> List[Dict[str, Any]]:
        r"""Executes a query and compiles the results.

        Args:
            query (str): Query string for information retriever.
            top_k (int, optional): The number of top results to return during
                retriever. Must be a positive integer. Defaults to
                `DEFAULT_TOP_K_RESULTS`.

        Returns:
            List[Dict[str]]: Concatenated list of the query results.

        Raises:
            ValueError: If `top_k` is less than or equal to 0, if the BM25
                model has not been initialized by calling `process`
                first.
        """
        import numpy as np

        if top_k <= 0:
            raise ValueError("top_k must be a positive integer.")
        if self.bm25 is None or not self.chunks:
            raise ValueError(
                "BM25 model is not initialized. Call `process` first."
            )

        # Preprocess query similarly to how documents were processed
        processed_query = _tokenize(query)
        # Retrieve documents based on BM25 scores
        scores = self.bm25.get_scores(processed_query)

        top_k_indices = np.argpartition(scores, -top_k)[-top_k:]

        formatted_results = []
        for i in top_k_indices:
            result_dict = {
                'similarity score': scores[i],
                'content path': self.content_input_path,
                'metadata': self.chunks[i].metadata.to_dict(),
                'text': str(self.chunks[i]),
            }
            formatted_results.append(result_dict)

        # Sort the list of dictionaries by 'similarity score' from high to low
        formatted_results.sort(
            key=lambda x: x['similarity score'], reverse=True
        )

        return formatted_results
