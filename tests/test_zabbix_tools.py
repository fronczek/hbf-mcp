"""Offline tests: Zabbix MCP tool is fixed, read-only, and never leaks errors."""
from __future__ import annotations

import asyncio
import json
import unittest
from subprocess import CompletedProcess
from unittest.mock import patch

import zabbix_tools as z


class ZabbixToolTests(unittest.TestCase):
    def test_bounded_window(self):
        for value in (-1, 0, 169, '24', True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                z.health_report(value)

    def test_only_fixed_local_command(self):
        data={'hosts':{'hbfplhpved01':{}}, 'alerts':{}}
        with patch.object(z.subprocess, 'run', return_value=CompletedProcess([],0,json.dumps(data),'token-shall-not-appear')) as run:
            report = z.health_report(24)
        self.assertEqual(report, data)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd, ['/usr/bin/python3','/home/gptagent/bin/zabbix-watch','--hours','24'])
        self.assertTrue(run.call_args.kwargs['capture_output'])

    def test_helper_failure_does_not_expose_secrets(self):
        with patch.object(z.subprocess,'run',return_value=CompletedProcess([],1,'token-secret','token-secret')):
            with self.assertRaisesRegex(RuntimeError,'helper failed') as error:
                z.health_report()
        self.assertNotIn('token-secret',str(error.exception))

    def test_tool_registration_readonly(self):
        from mcp.server import MCPServer
        server=MCPServer('Zabbix Tests')
        z.register_tools(server)
        tools=asyncio.run(server.list_tools())
        self.assertEqual([x.name for x in tools],['zabbix_health'])
        self.assertTrue(tools[0].annotations.read_only_hint)
        self.assertFalse(tools[0].annotations.destructive_hint)

if __name__ == '__main__':
    unittest.main()
