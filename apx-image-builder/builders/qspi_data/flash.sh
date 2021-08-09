#!/bin/bash

PARTITIONS='###PARTITIONS###'

cd "$(dirname "$(readlink -f "$0")")"

for PARTINFO in $PARTITIONS; do
	MTD=$(cut -d':' -f1 <<<"$PARTINFO")
	FILE=$(cut -d':' -f2- <<<"$PARTINFO")
	echo "Flashing ${FILE} to /dev/mtd${MTD}"
	if ! flashcp -v "$FILE" "/dev/mtd${MTD}"; then
		echo "flashcp failed with exit status $?"
		exit 1
	fi
done
echo 'FINISHED!'
exit 0
