# Crainbow CBT — PHASE 6G
## Local Server Deployment & Network Connection

This phase prepares the CBT for a school LAN where one Windows computer acts as the server and candidate computers connect through the local network.

## Recommended topology

Internet is NOT required during the examination once the application and database are on the server.

    SERVER COMPUTER
    192.168.1.x
         |
      Network
    / / / / /    PC1 PC2 PC3 ... PCn

The server computer runs the CBT application and database. Candidate computers open the server address in a web browser.

## 1. Choose the server computer

Use the most reliable available Windows computer.

Recommended:
- connected to AC power
- reliable network adapter
- preferably wired Ethernet
- do not use it as a candidate workstation
- prevent sleep during the examination
- keep it physically accessible to the administrator

## 2. Install Python

Install Python 3.x on the server if it is not already installed.

During installation, enable:
    Add Python to PATH

Then open Command Prompt and check:
    python --version

## 3. Create a virtual environment

From the CBT project folder:

    python -m venv .venv

Activate it:

    .venv\Scripts\activate

Install the project's requirements if a requirements.txt file is present:

    pip install -r requirements.txt

## 4. Start the server on the LAN

The application must listen on all local network interfaces, not only 127.0.0.1.

For Flask this normally means:

    host=0.0.0.0

Use the project's supplied Windows launcher where available.

The candidate-facing URL will normally be:

    http://SERVER_IP:PORT/

Example:

    http://192.168.1.10:5000/

Do NOT type 127.0.0.1 on a candidate computer. That address means "this computer".

## 5. Find the server IP

On the server:

    ipconfig

Look for the active network adapter and its IPv4 Address, for example:

    IPv4 Address . . . . . : 192.168.1.10

Record that address.

If possible, reserve this address in the router/DHCP settings so it does not change. For an isolated school LAN, a consistent address is especially useful.

## 6. Windows Firewall

Windows may block incoming connections to the application.

The safer approach is to create an inbound rule for the specific application port, preferably restricted to the local/private network.

Example for port 5000, from an Administrator Command Prompt:

    netsh advfirewall firewall add rule name="Crainbow CBT Local Server" dir=in action=allow protocol=TCP localport=5000 profile=private

If your application uses another port, replace 5000.

Only open the port on the PRIVATE/LAN profile. Do not expose the CBT port to the public internet.

## 7. Test the server itself

On the server browser:

    http://127.0.0.1:5000/

Then test using the server's LAN IP:

    http://SERVER_IP:5000/

Both should reach the application.

## 8. Test a candidate computer

On a candidate PC connected to the same network, open:

    http://SERVER_IP:5000/

The candidate computer must be on the same LAN/subnet as the server.

Example:

Server:
    192.168.1.10

Candidate:
    192.168.1.11

Then the candidate opens:

    http://192.168.1.10:5000/

## 9. Connectivity test

From a candidate PC, first test:

    ping SERVER_IP

If ping succeeds, that is useful evidence that the machines can see each other, but the application port still needs testing.

Then open the CBT URL in the browser.

If ping fails:
- check that both computers are connected to the same LAN
- check Wi-Fi isolation/client isolation on the router
- check Windows network profile
- check firewall rules
- check the server IP

If ping succeeds but the website does not open:
- verify the CBT server is running
- verify the port
- verify the firewall rule
- verify the server is listening on 0.0.0.0 rather than localhost only

## 10. Candidate-machine preparation

Before Tuesday, prepare every usable computer:

- connect to the same LAN
- open the CBT URL
- verify the login page loads
- use a test candidate account
- start an examination
- answer a few questions
- move backward and forward
- refresh once during a controlled test
- submit
- verify the result is recorded by the administrator

Do not use real candidate accounts for testing.

## 11. Eight-computer minimum setup

If only 8 computers are available:

- 1 = server/admin computer
- 7 = candidate computers

If additional computers are repaired:

- keep the same server
- connect additional candidate computers to the same LAN
- no change to the database is required merely because the number of candidate computers increases

The system is not limited to eight candidates. The physical number of simultaneous candidates is limited by the available client computers and the server/network capacity.

## 12. Important examination-day rule

The server must remain running for the entire examination session.

Do not:
- restart Windows
- close the CBT server
- change the server IP
- change the system clock
- disconnect the server from the network
- put the server to sleep

Keep a backup of the database before candidates begin.

## 13. Emergency procedure

If one candidate computer fails:

1. Do not delete the candidate's database record.
2. Keep the candidate's existing attempt intact.
3. Move the candidate to another working computer.
4. Log the candidate back into the same examination according to the configured recovery/attempt policy.
5. Do not create a second attempt casually.

The exact recovery policy should be tested before the live examination.

## 14. Final network rehearsal

Perform a full rehearsal with every available computer:

    Server starts
       ↓
    All client PCs connect
       ↓
    Test candidates log in
       ↓
    Examination starts independently
       ↓
    Candidates answer/change answers
       ↓
    One candidate submits manually
       ↓
    One candidate reaches expiry
       ↓
    Admin sees both completed attempts
       ↓
    Results are exported/backed up

Do this before loading real candidate data.

## 15. Security boundary

This setup is intended for a trusted local school network.

Do NOT:
- port-forward the CBT server to the internet
- expose the application publicly
- share the admin password with candidates
- use the server as a normal candidate workstation

The examination should remain inside the school's local network.
