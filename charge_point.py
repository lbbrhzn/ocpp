"""Virtual charge point instance used to test OCPP integration."""
import asyncio
import logging

from ocpp.exceptions import NotImplementedError, OCPPError
from ocpp.messages import CallError
from ocpp.v16 import ChargePoint as cp, call
from ocpp.v16.enums import RegistrationStatus
import websockets

logging.basicConfig(level=logging.INFO)


class ChargePoint(cp):
    """Implementation of an OCPP charge point."""

    async def send_boot_notification(self):
        """Send a BootNotification to the central system."""
        request = call.BootNotificationPayload(
            charge_point_model="Optimus", charge_point_vendor="The Mobility House"
        )

        try:
            response = await self.call(request)
        except OCPPError:
            return

        if response.status == RegistrationStatus.accepted:
            print("Connected to central system.")

    async def _handle_call(self, msg):
        try:
            await super()._handle_call(msg)
        except NotImplementedError as e:
            response = msg.create_call_error(e).to_json()
            await self._send(response)

    async def _get_specific_response(self, unique_id, timeout):
        resp = await super()._get_specific_response(unique_id, timeout)

        if isinstance(resp, CallError):
            raise resp.to_exception()

        return resp


async def main():
    """Entrypoint for the script."""
    async with websockets.connect(
        "ws://localhost:9000/CP_1", subprotocols=["ocpp1.6"]
    ) as ws:

        cp = ChargePoint("CP_1", ws)

        await asyncio.gather(cp.start(), cp.send_boot_notification())


if __name__ == "__main__":
    asyncio.run(main())
