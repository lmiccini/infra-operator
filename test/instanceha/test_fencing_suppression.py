"""
Tests for controller-driven fencing suppression via annotation (Improvement 3).

Covers:
- _check_fencing_suppressed returns (False, "") when annotation absent
- Returns (True, reason) when annotation present
- Handles K8s API errors gracefully (returns False — fail-open)
- Handles missing credentials (returns False)
- FENCING_SUPPRESSED Prometheus gauge
"""

import unittest
from unittest.mock import patch, MagicMock

import conftest  # noqa: F401
import instanceha


class TestCheckFencingSuppressed(unittest.TestCase):
    """Tests for _check_fencing_suppressed function."""

    @patch('instanceha.requests.get')
    @patch('instanceha._get_k8s_credentials', return_value=('token', 'openstack'))
    @patch.dict('os.environ', {'INSTANCEHA_CR_NAME': 'instanceha'})
    def test_returns_false_when_no_annotation(self, _creds, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                'metadata': {
                    'annotations': {}
                }
            }
        )
        suppressed, reason = instanceha._check_fencing_suppressed()
        self.assertFalse(suppressed)
        self.assertEqual(reason, "")

    @patch('instanceha.requests.get')
    @patch('instanceha._get_k8s_credentials', return_value=('token', 'openstack'))
    @patch.dict('os.environ', {'INSTANCEHA_CR_NAME': 'instanceha'})
    def test_returns_true_when_annotation_present(self, _creds, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                'metadata': {
                    'annotations': {
                        'instanceha.openstack.org/fencing-suppressed':
                            'node:worker-1 False|rabbitmq:rabbitmq-server-1 not ready'
                    }
                }
            }
        )
        suppressed, reason = instanceha._check_fencing_suppressed()
        self.assertTrue(suppressed)
        self.assertIn('worker-1', reason)
        self.assertIn('rabbitmq', reason)

    @patch('instanceha.requests.get')
    @patch('instanceha._get_k8s_credentials', return_value=('token', 'openstack'))
    @patch.dict('os.environ', {'INSTANCEHA_CR_NAME': 'instanceha'})
    def test_returns_false_on_api_error(self, _creds, mock_get):
        mock_get.side_effect = ConnectionError("connection refused")
        suppressed, reason = instanceha._check_fencing_suppressed()
        self.assertFalse(suppressed)
        self.assertEqual(reason, "")

    @patch('instanceha.requests.get')
    @patch('instanceha._get_k8s_credentials', return_value=('token', 'openstack'))
    @patch.dict('os.environ', {'INSTANCEHA_CR_NAME': 'instanceha'})
    def test_returns_false_on_non_200(self, _creds, mock_get):
        mock_get.return_value = MagicMock(status_code=404)
        suppressed, reason = instanceha._check_fencing_suppressed()
        self.assertFalse(suppressed)

    @patch('instanceha._get_k8s_credentials', return_value=(None, None))
    def test_returns_false_when_no_credentials(self, _creds):
        suppressed, reason = instanceha._check_fencing_suppressed()
        self.assertFalse(suppressed)
        self.assertEqual(reason, "")

    @patch.dict('os.environ', {}, clear=False)
    @patch('instanceha._get_k8s_credentials', return_value=('token', 'openstack'))
    def test_returns_false_when_no_cr_name(self, _creds):
        import os
        os.environ.pop('INSTANCEHA_CR_NAME', None)
        suppressed, reason = instanceha._check_fencing_suppressed()
        self.assertFalse(suppressed)

    @patch('instanceha.requests.get')
    @patch('instanceha._get_k8s_credentials', return_value=('token', 'openstack'))
    @patch.dict('os.environ', {'INSTANCEHA_CR_NAME': 'instanceha'})
    def test_returns_false_when_no_annotations_key(self, _creds, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                'metadata': {}
            }
        )
        suppressed, reason = instanceha._check_fencing_suppressed()
        self.assertFalse(suppressed)

    @patch('instanceha.requests.get')
    @patch('instanceha._get_k8s_credentials', return_value=('token', 'openstack'))
    @patch.dict('os.environ', {'INSTANCEHA_CR_NAME': 'instanceha'})
    def test_queries_correct_url(self, _creds, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {'metadata': {'annotations': {}}}
        )
        instanceha._check_fencing_suppressed()
        call_args = mock_get.call_args
        url = call_args[1].get('url', call_args[0][0] if call_args[0] else '')
        self.assertIn('instanceha.openstack.org', url)
        self.assertIn('instancehas/instanceha', url)


class TestFencingSuppressedMetric(unittest.TestCase):
    """Tests for FENCING_SUPPRESSED gauge."""

    def test_metric_exists(self):
        self.assertIsNotNone(instanceha.FENCING_SUPPRESSED)


class TestFencingSuppressedAnnotationConstant(unittest.TestCase):
    """Tests for the annotation key constant."""

    def test_annotation_key_defined(self):
        self.assertEqual(
            instanceha._FENCING_SUPPRESSED_ANNOTATION,
            'instanceha.openstack.org/fencing-suppressed')


if __name__ == '__main__':
    unittest.main()
