"""Issue #11 × issue #18: deleting a robot also drops this computer's records of unfinished file
transfers to it (``<workspace>/transfers/``), which name the device but hold no login. Nothing is
contacted: what a transfer left on the device stays there.

Every machine here is a local SSH protocol fixture: nothing reaches a real one.
"""
import json
import pathlib
import unittest

from brainos import device_transfer_record as records
from test_robot_remove import RemoveCase


class TransferRecordTests(RemoveCase):
    def transfer_record(self, connection_id, name='pkg.whl'):
        """A record as an interrupted transfer leaves it: one JSON file in the device's folder."""
        folder = records.device_folder(pathlib.Path(self.store.root) / 'transfers', connection_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f'{name}.json'
        path.write_text(json.dumps({'target': f'~/neuxmind-pkg/{name}', 'sent': 1024}))
        return folder

    def test_the_records_of_unfinished_transfers_go_with_the_robot(self):
        connection = self.connect_directly()
        folder = self.transfer_record(connection['id'])

        self.remove(connection['robot_id'])

        self.assertFalse(folder.exists(), 'the transfer records of the deleted robot were left behind')

    def test_another_robots_transfer_records_stay(self):
        connection = self.connect_directly()
        other = self.transfer_record('device-connection-of-another-robot')

        self.remove(connection['robot_id'])

        self.assertTrue((other / 'pkg.whl.json').exists())

    def test_a_robot_without_transfers_is_deleted_as_before(self):
        connection = self.connect_directly()

        result = self.remove(connection['robot_id'])

        self.assertEqual(result['deleted'], {'device_connections': 1})
        self.assertFalse((pathlib.Path(self.store.root) / 'transfers').exists())


if __name__ == '__main__':
    unittest.main()
