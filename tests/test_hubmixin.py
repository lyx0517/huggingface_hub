import os
import shutil
import time
import unittest
import uuid
from io import BytesIO

from huggingface_hub import HfApi
from huggingface_hub.file_download import is_torch_available
from huggingface_hub.hub_mixin import PyTorchModelHubMixin
from huggingface_hub.utils import logging

from .testing_constants import ENDPOINT_STAGING, TOKEN, USER
from .testing_utils import set_write_permission_and_retry


def repo_name(id=uuid.uuid4().hex[:6]):
    return "mixin-repo-{0}-{1}".format(id, int(time.time() * 10e3))


logger = logging.get_logger(__name__)
WORKING_REPO_SUBDIR = "fixtures/working_repo_2"
WORKING_REPO_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), WORKING_REPO_SUBDIR
)

if is_torch_available():
    import torch.nn as nn


def require_torch(test_case):
    """
    Decorator marking a test that requires PyTorch.

    These tests are skipped when PyTorch isn't installed.

    """
    if not is_torch_available():
        return unittest.skip("test requires PyTorch")(test_case)
    else:
        return test_case


if is_torch_available():

    class DummyModel(nn.Module, PyTorchModelHubMixin):
        def __init__(self, **kwargs):
            super().__init__()
            self.config = kwargs.pop("config", None)
            self.l1 = nn.Linear(2, 2)

        def forward(self, x):
            return self.l1(x)

else:
    DummyModel = None


@require_torch
class HubMixingCommonTest(unittest.TestCase):
    _api = HfApi(endpoint=ENDPOINT_STAGING)


@require_torch
class HubMixingTest(HubMixingCommonTest):
    def tearDown(self) -> None:
        if os.path.exists(WORKING_REPO_DIR):
            shutil.rmtree(WORKING_REPO_DIR, onerror=set_write_permission_and_retry)
        logger.info(
            f"Does {WORKING_REPO_DIR} exist: {os.path.exists(WORKING_REPO_DIR)}"
        )

    @classmethod
    def setUpClass(cls):
        """
        Share this valid token in all tests below.
        """
        cls._token = TOKEN
        cls._api.set_access_token(TOKEN)

    def test_save_pretrained(self):
        REPO_NAME = repo_name("save")
        model = DummyModel()

        model.save_pretrained(f"{WORKING_REPO_DIR}/{REPO_NAME}")
        files = os.listdir(f"{WORKING_REPO_DIR}/{REPO_NAME}")
        self.assertTrue("pytorch_model.bin" in files)
        self.assertEqual(len(files), 1)

        model.save_pretrained(
            f"{WORKING_REPO_DIR}/{REPO_NAME}", config={"num": 12, "act": "gelu"}
        )
        files = os.listdir(f"{WORKING_REPO_DIR}/{REPO_NAME}")
        self.assertTrue("config.json" in files)
        self.assertTrue("pytorch_model.bin" in files)
        self.assertEqual(len(files), 2)

    def test_rel_path_from_pretrained(self):
        model = DummyModel()
        model.save_pretrained(
            f"tests/{WORKING_REPO_SUBDIR}/FROM_PRETRAINED",
            config={"num": 10, "act": "gelu_fast"},
        )

        model = DummyModel.from_pretrained(
            f"tests/{WORKING_REPO_SUBDIR}/FROM_PRETRAINED"
        )
        self.assertTrue(model.config == {"num": 10, "act": "gelu_fast"})

    def test_abs_path_from_pretrained(self):
        REPO_NAME = repo_name("FROM_PRETRAINED")
        model = DummyModel()
        model.save_pretrained(
            f"{WORKING_REPO_DIR}/{REPO_NAME}",
            config={"num": 10, "act": "gelu_fast"},
        )

        model = DummyModel.from_pretrained(f"{WORKING_REPO_DIR}/{REPO_NAME}")
        self.assertDictEqual(model.config, {"num": 10, "act": "gelu_fast"})

    def test_push_to_hub(self):
        REPO_NAME = repo_name("PUSH_TO_HUB")
        model = DummyModel()
        model.push_to_hub(
            repo_path_or_name=f"{WORKING_REPO_DIR}/{REPO_NAME}",
            api_endpoint=ENDPOINT_STAGING,
            use_auth_token=self._token,
            git_user="ci",
            git_email="ci@dummy.com",
            config={"num": 7, "act": "gelu_fast"},
        )

        model_info = self._api.model_info(f"{USER}/{REPO_NAME}", token=self._token)
        self.assertEqual(model_info.modelId, f"{USER}/{REPO_NAME}")

        self._api.delete_repo(repo_id=f"{REPO_NAME}", token=self._token)

    def test_push_to_hub_with_other_files(self):
        REPO_A = repo_name("with_files")
        REPO_B = repo_name("without_files")
        self._api.create_repo(token=self._token, name=REPO_A)
        self._api.create_repo(token=self._token, name=REPO_B)
        for i in range(5):
            self._api.upload_file(
                # Each are .5mb in size
                path_or_fileobj=BytesIO(os.urandom(500000)),
                path_in_repo=f"temp/new_file_{i}.bytes",
                repo_id=f"{USER}/{REPO_A}",
                token=self._token,
            )
        model = DummyModel()
        start_time = time.time()
        model.push_to_hub(
            repo_path_or_name=f"{WORKING_REPO_SUBDIR}/{REPO_A}",
            api_endpoint=ENDPOINT_STAGING,
            use_auth_token=self._token,
            git_user="ci",
            git_email="ci@dummy.com",
            config={"num": 7, "act": "gelu_fast"},
        )
        REPO_A_TIME = start_time - time.time()

        start_time = time.time()
        model.push_to_hub(
            repo_path_or_name=f"{USER}/{REPO_B}",
            api_endpoint=ENDPOINT_STAGING,
            use_auth_token=self._token,
            git_user="ci",
            git_email="ci@dummy.com",
            config={"num": 7, "act": "gelu_fast"},
        )
        REPO_B_TIME = start_time - time.time()
        # Less than half a second from each other
        self.assertLess(REPO_A_TIME - REPO_B_TIME, 0.5)

        self._api.delete_repo(name=REPO_A, token=self._token)
        self._api.delete_repo(name=REPO_B, token=self._token)
