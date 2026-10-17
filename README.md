# Vision OR headset builds

Install page: https://3m.github.io/visionor-headset/

Development builds of Vision OR for Apple Vision Pro, published for headsets registered with the Vision OR team.
They install only on those headsets.

A GitHub Action runs `publish.py` every 15 minutes. It adds each new build from Xcode Cloud as a release and lists it on the page.
Don't edit this repo by hand: its source lives in the private Vision OR repo, and `sync-site.sh` there updates it.
