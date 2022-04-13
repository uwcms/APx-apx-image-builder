# rpmbuild --define "_builddir ." --target aarch64 -bb ./binkernel.spec
%define kernelrelease_underscore %(echo -n '%{kernelrelease}' | sed -e 's/-/_/g')
Name: apx-kernel
Summary: The Linux Kernel (APx Build)
Version: %{kernelrelease_underscore}
Release: %{rpm_release}
License: GPL
Group: System Environment/Kernel
Vendor: The Linux Community
URL: http://www.kernel.org
Provides: apx-kernel-%{kernelrelease}
Provides: kernel-%{kernelrelease}
Provides: apx-kernel-modules = %{version}
Provides: kernel-modules = %{version}
Obsoletes: kernel
Obsoletes: kernel-modules
# APx: These all provide hook scripts that are unnecessary and make kernel install harder.
Conflicts: grubby grubby-deprecated grub2-common
# APx: We use mkimage in our post script.
Requires: uboot-tools

%define __spec_install_post /usr/lib/rpm/brp-compress || :
%define debug_package %{nil}

%description
The Linux Kernel, the operating system core itself

%package headers
Summary: Header files for the Linux kernel for use by glibc
Group: Development/System
Obsoletes: kernel-headers
Provides: apx-kernel-headers = %{version}
Provides: kernel-headers = %{version}
%description headers
Kernel-headers includes the C header files that specify the interface
between the Linux kernel and userspace libraries and programs.  The
header files define structures and constants that are needed for
building most standard programs and are also needed for rebuilding the
glibc package.

%install
mkdir -p %{buildroot}/boot
%ifarch ia64
mkdir -p %{buildroot}/boot/efi
cp $(make -f ./Makefile %{kernel_makeargs} image_name) %{buildroot}/boot/efi/vmlinuz-%{kernelrelease}
ln -s efi/vmlinuz-%{kernelrelease} %{buildroot}/boot/
%else
cp $(make -f ./Makefile %{kernel_makeargs} image_name) %{buildroot}/boot/vmlinuz-%{kernelrelease}
%endif
# APx: aarch64 u-boot is having trouble with these images being compressed, at this time.
%ifarch aarch64
gzip -d -c %{buildroot}/boot/vmlinuz-%{kernelrelease} > %{buildroot}/boot/vmlinux-%{kernelrelease}
%endif
make -f ./Makefile %{?_smp_mflags} %{kernel_makeargs} INSTALL_MOD_PATH=%{buildroot} modules_install
make -f ./Makefile %{?_smp_mflags} %{kernel_makeargs} INSTALL_HDR_PATH=%{buildroot}/usr headers_install
cp System.map %{buildroot}/boot/System.map-%{kernelrelease}
cp .config %{buildroot}/boot/config-%{kernelrelease}
# APx: Save disk space and installation time.
#bzip2 -9 --keep vmlinux
#mv vmlinux.bz2 %{buildroot}/boot/vmlinux-%{kernelrelease}.bz2
# APx: Inject a little "update-bootargs" script.
mkdir -p %{buildroot}/usr/sbin
echo '#!/bin/bash'  > %{buildroot}/usr/sbin/update-bootargs
echo 'if ! [ -e /etc/bootargs ]; then echo "/etc/bootargs not found."; exit 1; fi' >> %{buildroot}/usr/sbin/update-bootargs
echo 'mkimage -c none -A arm -T script -d /etc/bootargs /boot/bootargs.scr' >> %{buildroot}/usr/sbin/update-bootargs
chmod 755 %{buildroot}/usr/sbin/update-bootargs

%clean
rm -rf %{buildroot}

%post
# APx: We don't ship dtbs with our kernel.
[ -e /etc/u-boot.conf ] || echo 'FIRMWAREDT=True' > /etc/u-boot.conf
#
if [ -x /sbin/installkernel -a -r /boot/vmlinuz-%{kernelrelease} -a -r /boot/System.map-%{kernelrelease} ]; then
cp /boot/vmlinuz-%{kernelrelease} /boot/.vmlinuz-%{kernelrelease}-rpm
cp /boot/System.map-%{kernelrelease} /boot/.System.map-%{kernelrelease}-rpm
rm -f /boot/vmlinuz-%{kernelrelease} /boot/System.map-%{kernelrelease}
/sbin/installkernel %{kernelrelease} /boot/.vmlinuz-%{kernelrelease}-rpm /boot/.System.map-%{kernelrelease}-rpm
rm -f /boot/.vmlinuz-%{kernelrelease}-rpm /boot/.System.map-%{kernelrelease}-rpm
fi
# APx: Make it easy for u-boot to find the last installed kernel.
ln -snf vmlinuz-%{kernelrelease} /boot/vmlinuz
if [ -e /boot/vmlinux-%{kernelrelease} ]; then
	ln -snf vmlinux-%{kernelrelease} /boot/vmlinux
else
	rm -f /boot/vmlinux
fi
# APx: Build default bootargs
if [ ! -e /etc/bootargs ]; then
	ROOTDEV="$(awk '/^[^#]/ { if ($2 == "/") { print $1 } }' /etc/fstab)"
	ROOTDEV="$(sed -re 's/"//g' <<<"$ROOTDEV")"
	echo 'setenv bootargs "earlyprintk uio_pdrv_genirq.of_id=generic-uio root='"${ROOTDEV}"' rootdelay=1 selinux=0 verbose"' > /etc/bootargs
fi
/usr/sbin/update-bootargs

%preun
# APx: We don't ship dtbs with our kernel.
[ -e /etc/u-boot.conf ] || echo 'FIRMWAREDT=True' > /etc/u-boot.conf
#
if [ -x /sbin/new-kernel-pkg ]; then
new-kernel-pkg --remove %{kernelrelease} --rminitrd --initrdfile=/boot/initramfs-%{kernelrelease}.img
elif [ -x /usr/bin/kernel-install ]; then
kernel-install remove %{kernelrelease}
fi

%postun
if [ -x /sbin/update-bootloader ]; then
/sbin/update-bootloader --remove %{kernelrelease}
fi

%files
%defattr (-, root, root)
/lib/modules/%{kernelrelease}
%exclude /lib/modules/%{kernelrelease}/build
%exclude /lib/modules/%{kernelrelease}/source
/boot/*
# APx: Inject a little "update-bootargs" script.
/usr/sbin/update-bootargs

%files headers
%defattr (-, root, root)
/usr/include
