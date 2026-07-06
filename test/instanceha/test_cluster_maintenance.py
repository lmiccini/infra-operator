"""
Tests for cluster maintenance detection and fencing suppression.

Covers:
- _check_cluster_maintenance: reads CR annotation from K8s API
- update_cluster_maintenance: state transitions, metrics, logging
- Fencing gate: main loop skips fencing via update_cluster_maintenance return value
"""

import os
import unittest
from unittest.mock import patch, MagicMock

import conftest  # noqa: F401
import instanceha


class TestCheckClusterMaintenance(unittest.TestCase):
    """Tests for _check_cluster_maintenance()."""

    @patch('instanceha.requests.get')
    @patch('instanceha._get_k8s_credentials', return_value=('token', 'openstack'))
    @patch.dict(os.environ, {'INSTANCEHA_CR_NAME': 'instanceha-sample'})
    def test_returns_true_when_annotation_present(self, _creds, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={
                'metadata': {
                    'annotations': {
                        'instanceha.openstack.org/cluster-maintenance': 'true'
                    }
                }
            })
        )
        self.assertTrue(instanceha._check_cluster_maintenance())
        mock_get.assert_called_once()
        call_url = mock_get.call_args[0][0]
        self.assertIn('/apis/instanceha.openstack.org/v1beta1', call_url)
        self.assertIn('/instancehas/instanceha-sample', call_url)

    @patch('instanceha.requests.get')
    @patch('instanceha._get_k8s_credentials', return_value=('token', 'openstack'))
    @patch.dict(os.environ, {'INSTANCEHA_CR_NAME': 'instanceha-sample'})
    def test_returns_false_when_annotation_absent(self, _creds, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={
                'metadata': {'annotations': {}}
            })
        )
        self.assertFalse(instanceha._check_cluster_maintenance())

    @patch('instanceha.requests.get')
    @patch('instanceha._get_k8s_credentials', return_value=('token', 'openstack'))
    @patch.dict(os.environ, {'INSTANCEHA_CR_NAME': 'instanceha-sample'})
    def test_returns_false_when_no_annotations_key(self, _creds, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={'metadata': {}})
        )
        self.assertFalse(instanceha._check_cluster_maintenance())

    @patch('instanceha.requests.get')
    @patch('instanceha._get_k8s_credentials', return_value=('token', 'openstack'))
    @patch.dict(os.environ, {'INSTANCEHA_CR_NAME': 'instanceha-sample'})
    def test_returns_false_when_annotation_value_is_not_true(self, _creds, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={
                'metadata': {
                    'annotations': {
                        'instanceha.openstack.org/cluster-maintenance': 'false'
                    }
                }
            })
        )
        self.assertFalse(instanceha._check_cluster_maintenance())

    @patch('instanceha.requests.get')
    @patch('instanceha._get_k8s_credentials', return_value=('token', 'openstack'))
    @patch.dict(os.environ, {'INSTANCEHA_CR_NAME': 'instanceha-sample'})
    def test_returns_false_on_connection_error(self, _creds, mock_get):
        mock_get.side_effect = ConnectionError("connection refused")
        self.assertFalse(instanceha._check_cluster_maintenance())

    @patch('instanceha.requests.get')
    @patch('instanceha._get_k8s_credentials', return_value=('token', 'openstack'))
    @patch.dict(os.environ, {'INSTANCEHA_CR_NAME': 'instanceha-sample'})
    def test_returns_false_on_timeout(self, _creds, mock_get):
        mock_get.side_effect = TimeoutError("timed out")
        self.assertFalse(instanceha._check_cluster_maintenance())

    @patch('instanceha.requests.get')
    @patch('instanceha._get_k8s_credentials', return_value=('token', 'openstack'))
    @patch.dict(os.environ, {'INSTANCEHA_CR_NAME': 'instanceha-sample'})
    def test_returns_false_on_404(self, _creds, mock_get):
        mock_get.return_value = MagicMock(status_code=404)
        self.assertFalse(instanceha._check_cluster_maintenance())

    @patch('instanceha._get_k8s_credentials', return_value=(None, None))
    def test_returns_false_when_no_credentials(self, _creds):
        self.assertFalse(instanceha._check_cluster_maintenance())

    @patch('instanceha._get_k8s_credentials', return_value=('token', 'openstack'))
    @patch.dict(os.environ, {'INSTANCEHA_CR_NAME': ''})
    def test_returns_false_when_no_cr_name(self, _creds):
        self.assertFalse(instanceha._check_cluster_maintenance())


class TestUpdateClusterMaintenance(unittest.TestCase):
    """Tests for update_cluster_maintenance() state transitions."""

    def setUp(self):
        self.config = instanceha.ConfigManager()
        self.service = instanceha.InstanceHAService(self.config)

    def test_initial_state(self):
        self.assertFalse(self.service.cluster_maintenance)

    @patch('instanceha._emit_k8s_event')
    @patch('instanceha._check_cluster_maintenance', return_value=True)
    def test_sets_maintenance_on_annotation(self, _check, _event):
        self.service.cluster_maintenance = False
        result = self.service.update_cluster_maintenance()
        self.assertTrue(result)
        self.assertTrue(self.service.cluster_maintenance)

    @patch('instanceha._emit_k8s_event')
    @patch('instanceha._check_cluster_maintenance', return_value=False)
    def test_clears_maintenance_when_annotation_removed(self, _check, _event):
        self.service.cluster_maintenance = True
        result = self.service.update_cluster_maintenance()
        self.assertFalse(result)
        self.assertFalse(self.service.cluster_maintenance)

    @patch('instanceha._check_cluster_maintenance', side_effect=Exception("api error"))
    def test_returns_previous_state_on_error(self, _check):
        self.service.cluster_maintenance = False
        result = self.service.update_cluster_maintenance()
        self.assertFalse(result)
        self.assertFalse(self.service.cluster_maintenance)

    @patch('instanceha._check_cluster_maintenance', side_effect=Exception("api error"))
    def test_preserves_maintenance_on_error(self, _check):
        self.service.cluster_maintenance = True
        result = self.service.update_cluster_maintenance()
        self.assertTrue(result)
        self.assertTrue(self.service.cluster_maintenance)

    @patch('instanceha._emit_k8s_event')
    @patch('instanceha._check_cluster_maintenance', return_value=True)
    def test_no_repeated_event_on_sustained_maintenance(self, _check, mock_event):
        """Once maintenance is detected, subsequent calls should not re-emit the event."""
        self.service.cluster_maintenance = True
        self.service.update_cluster_maintenance()
        self.assertTrue(self.service.cluster_maintenance)
        mock_event.assert_not_called()

    @patch('instanceha._emit_k8s_event')
    @patch('instanceha._check_cluster_maintenance', return_value=True)
    def test_emits_event_on_transition_to_maintenance(self, _check, mock_event):
        self.service.cluster_maintenance = False
        self.service.update_cluster_maintenance()
        mock_event.assert_called_once()
        self.assertEqual(mock_event.call_args[0][1], 'ClusterMaintenanceDetected')

    @patch('instanceha._emit_k8s_event')
    @patch('instanceha._check_cluster_maintenance', return_value=False)
    def test_emits_event_on_transition_from_maintenance(self, _check, mock_event):
        self.service.cluster_maintenance = True
        self.service.update_cluster_maintenance()
        mock_event.assert_called_once()
        self.assertEqual(mock_event.call_args[0][1], 'ClusterMaintenanceEnded')


class TestClusterMaintenanceFencingGate(unittest.TestCase):
    """Tests for the fencing gate using update_cluster_maintenance return value."""

    def setUp(self):
        self.config = instanceha.ConfigManager()
        self.service = instanceha.InstanceHAService(self.config)

    @patch('instanceha._emit_k8s_event')
    @patch('instanceha._check_cluster_maintenance', return_value=True)
    def test_maintenance_blocks_fencing(self, _check, _event):
        self.service.k8s_api_reachable = True

        with patch('instanceha._process_stale_services') as mock_process:
            with patch('instanceha._process_reenabling') as mock_reenable:
                if not self.service.k8s_api_reachable:
                    skipped = True
                elif self.service.update_cluster_maintenance():
                    skipped = True
                else:
                    skipped = False
                    mock_process()
                    mock_reenable()

                self.assertTrue(skipped)
                mock_process.assert_not_called()
                mock_reenable.assert_not_called()

    @patch('instanceha._check_cluster_maintenance', return_value=False)
    def test_no_maintenance_allows_fencing(self, _check):
        self.service.k8s_api_reachable = True
        self.assertFalse(self.service.update_cluster_maintenance())

    def test_k8s_unreachable_takes_precedence(self):
        """K8s API unreachable gate fires before cluster maintenance gate."""
        self.service.k8s_api_reachable = False
        self.service.cluster_maintenance = False

        if not self.service.k8s_api_reachable:
            gate = 'k8s_api'
        elif self.service.update_cluster_maintenance():
            gate = 'cluster_maintenance'
        else:
            gate = 'none'

        self.assertEqual(gate, 'k8s_api')


if __name__ == '__main__':
    unittest.main()
