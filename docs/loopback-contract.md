# v4l2loopback contract and guard (#86)

StreamStack Camera (`/dev/video43`) and the Canon node (`/dev/video42`) are v4l2loopback devices with
`exclusive_caps=1`: Chromium, Edge, Teams and Zoom list a node only while a writer is attached. This page is what
`streamstack doctor` and `streamstack loopback guard` check, and how to fix each finding.

## Kernel and module guard

After a kernel update the module can be missing for the new kernel (DKMS did not build it) and the nodes vanish at
the next reboot. `hardware/kmod.py` reads, without touching any device, `modules.dep` of the running and the newest
installed kernel, the headers, and `dkms status` when dkms exists, and reports `v4l2loopback-kernel` (module missing
or not built for a kernel), `v4l2loopback-pending` (installed kernel newer than the running one) and
`v4l2loopback-version` (loaded version below the recommended 0.15.4, which fixes two frame-copy concurrency bugs).
Each row carries the fix, for example `sudo dkms autoinstall -k <kernel>` and a reload only when no call is running.

Prevention: `make install` installs `/etc/kernel/postinst.d/zz-streamstack-v4l2loopback`, which runs
`dkms autoinstall -k $1` for every new kernel and never fails the kernel install. Hosts with a prebuilt module
(Ubuntu `ubuntu/dkms/v4l2loopback`) pass the check without dkms.

## Format contract for Chromium

`hardware/contract.py` checks the node the program camera writes: pixel format YUYV or NV12, one fixed size between
640x360 and 3840x2160 with even sides, 24 to 60 fps, and `exclusive_caps` on. Anything else is `v4l2loopback-contract`
with the offending value. The engine already writes yuyv422 at its configured size and rate.

## Competing producers

`hardware/contention.py` lists the processes holding a node by reading `/proc/<pid>/fd` only. A second writer on
`/dev/video43` (a leftover OBS virtual camera, a stray ffmpeg) is `fail` and names the program and pid, because only one
writer is allowed and the second one blocks or breaks the first. Two feeders on `/dev/video42` is `fail`; another
process holding the Cam Link is `warn`. Fix for OBS: close OBS, or choose Tools > Stop Virtual Camera.
`streamstack loopback guard` prints the rows and exits 1 on any fail.

## After a module reload

Reload only with the Console stopped and no call running, then run `streamstack loopback guard` and
`streamstack doctor`. The guard never reloads anything itself.
