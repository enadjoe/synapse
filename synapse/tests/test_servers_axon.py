import hashlib

import synapse.common as s_common
import synapse.telepath as s_telepath

import synapse.servers.axon as s_s_axon

import synapse.tests.utils as s_t_utils

asdfhash = hashlib.sha256(b'asdfasdf').digest()

class AxonServerTest(s_t_utils.SynTest):

    async def test_server(self):

        with self.getTestDir() as dirn:

            outp = self.getTestOutp()

            argv = [dirn, '--telepath', 'tcp://127.0.0.1:0/', '--https', '0']
            async with await s_s_axon.main(argv, outp=outp) as axon:
                async with axon.getLocalProxy() as proxy:
                    async with await proxy.upload() as fd:
                        await fd.write(b'asdfasdf')
                        await fd.save()

            # And data persists...
            async with await s_s_axon.main(argv, outp=outp) as axon:
                async with axon.getLocalProxy() as proxy:
                    self.true(await proxy.has(asdfhash))
