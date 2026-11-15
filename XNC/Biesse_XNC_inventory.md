# Biesse XNC — Archive Inventory

Part of [Biesse Rover 336 Playground](https://github.com/sergey-automation/Biesse-Rover-336-Playground).

This inventory describes the collected original XNC software, technical documentation and system recovery materials. It records file contents and verification status; it does not claim that the software has been successfully installed or tested on our machine.

## Archive structure

```text
XNC/
├── Biesse_XNC_inventory.md
├── Manuals/
│   ├── AU-103-98R.7z
│   └── XNC_Manuals.7z
├── Software/
│   ├── Plc040403.1_1
│   ├── Rover 322 73610
│   └── XNC1202.7z
└── System-Recovery/
    ├── 1200up8.GHO
    ├── 7361000_BAC_00_ING.zip
    ├── COPYQM.zip
    ├── RV HOW TO Create an HD XNC using Ghost.pdf
    └── XNC Установка Rv336 73787.txt
```

`1200up8.GHO` is an additional file intended for this collection; it was not included in the ZIP archive checked on 2026-11-15.

## Manuals

| File | Size (bytes) | Contents |
| --- | ---: | --- |
| [AU-103-98R.7z](Manuals/AU-103-98R.7z) | 11,844 | Instruction for adding or removing the C axis. |
| [XNC_Manuals.7z](Manuals/XNC_Manuals.7z) | 27,771,768 | XNC 1.2/2 documentation, programming, networking, UNIX, BIOS and hard disk documentation. |

## Software

| File | Size (bytes) | Contents |
| --- | ---: | --- |
| [XNC1202.7z](Software/XNC1202.7z) | 25,330,015 | XNC 1.2.0.2 installation software, Upgrade 5, DATI machine data, PLC files, installer and patches. |
| [Plc040403.1_1](Software/Plc040403.1_1) | 424,109 | GZIP-compressed TAR archive containing `home/xnc/bin/qplc`. |
| [Rover 322 73610](Software/Rover%20322%2073610) | 152 | Text record of software versions for Rover 322, identifier 73610. |

### XNC1202.7z contents

The previous archive inspection identified the following components:

- Base XNC 1.2.0.2 installation: 12 DOS disk parts, `V1202_12.01` through `V1202_12.12`. The combined GZIP/TAR stream was extracted; 2,481 TAR entries were recorded.
- Upgrade 5: five DOS disk parts, `up5_1202.1_5` through `up5_1202.5_5`. The combined stream was extracted; 524 TAR entries were recorded.
- DATI and PLC for Upgrade 5: `datip603.1_1`, `plc611.1_1`, `inst1_12.1_1`, `patch5.1_1`, `patch5_2.1_1`.
- Standalone installer `inst1_12.1_1`, `Qplccopy.1_1`, and color patches.
- `Link Rover XNC-NC1000.doc`: document AU-004/00N, revision 1.9, dated 28 March 2003.

The extracted machine data include Rover 336 variants and DXF drawings. The original archives are retained rather than replacing them with extracted copies.

### Rover 322 73610 version record

| Field | Recorded value |
| --- | --- |
| DAT Version | `00.04.04.01.UNI` |
| NC Language | `ING` |
| PLC Version | `00.04.04.01` |
| SYS Version | `0.0.0.5` |
| UPG Version | `7` |
| Control type | `XNC 1.2.0.0` |

These values describe the supplied Rover 322 record, not a verified configuration of our Rover 336.

## System recovery

| File | Size (bytes) | Contents / status |
| --- | ---: | --- |
| `1200up8.GHO` | 134,668,883 | Norton Ghost disk image, format Z2 identified during previous inspection. Not restored or internally verified. |
| [7361000_BAC_00_ING.zip](System-Recovery/7361000_BAC_00_ING.zip) | 1,342,256 | Contains CopyQM floppy image `73610.a`. Internal filesystem not extracted. |
| [COPYQM.zip](System-Recovery/COPYQM.zip) | 566,559 | CopyQM utility, including `COPYQM.EXE`, `CQMENU.EXE`, and instructions in English and Italian. |
| [RV HOW TO Create an HD XNC using Ghost.pdf](System-Recovery/RV%20HOW%20TO%20Create%20an%20HD%20XNC%20using%20Ghost.pdf) | 334,906 | Biesse instruction for creating an XNC hard disk using Ghost; six pages, revision 2, dated 21 June 2011. |
| [XNC Установка Rv336 73787.txt](System-Recovery/XNC%20Установка%20Rv336%2073787.txt) | 4,683 | Russian-language recovery procedure for Rover 336, identifier 73787. Describes restoring the Ghost image, preparing a machine-data floppy, installing PLC data and selecting the machine configuration. |

The Russian recovery instructions refer to `1200UP8.GHO`, `73610.a`, `Plc040403.1_1` and the command `lnk 336.4`. These are statements from the supplied instructions, not results of our own machine tests.

## Verification status

- The supplied `XNC(1).zip` was inspected on 15 November 2026. All nine contained files passed the ZIP CRC integrity check; their combined uncompressed size is 55,786,292 bytes.
- Earlier inspection extracted the XNC installation streams, nested archives and PLC package, and identified the Ghost image format.
- The Ghost image was not part of the current ZIP inspection. Its size and format are recorded from the earlier inventory.
- Successful installation, booting, compatibility with our machine and operation of the original control system have not been established by this inventory.
- The original file names are retained. File presence and archive integrity do not establish operational compatibility.

## Sources and acknowledgements

Some materials in this collection were obtained from files and information shared by members of online CNC communities. Related forum discussions used as research references include:

- [RuTracker — forum topic 4595579](https://rutracker.org/forum/viewtopic.php?t=4595579)
- [Tecnocom-UG — forum discussion, post 26120](https://forum.tecnocom-ug.ru/viewtopic.php?p=26120)

The precise original uploader and download source have not been established for every individual file. The links above are related research references, not a verified file-by-file provenance record.

Many thanks to the engineers, machine owners, service technicians and forum members who took the time to share these manuals, software archives, recovery instructions and practical experience over the years. Their contributions have helped preserve technical knowledge about older Biesse machines and made further investigation possible.

Original software and manufacturer documentation remain attributed to their respective authors and rights holders. This inventory is a record of collected materials, not a claim of authorship of those materials.
