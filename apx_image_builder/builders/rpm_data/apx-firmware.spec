# rpmbuild --define "_builddir ." --target aarch64 -bb ./binkernel.spec
Name: %{imagename}-firmware
Summary: %{imagename} ZYNQ firmware & bootloaders
Version: %{rpm_version}
Release: %{rpm_release}
License: Proprietary
Group: System Environment/Kernel
Vendor: %{vendor}
URL: https://github.com/uwcms/APx-apx-image-builder
Provides: zynqimage
Conflicts: zynqimage

%define __spec_install_post /usr/lib/rpm/brp-compress || :
%define debug_package %{nil}

%description
An early-boot firmware image for %{imagename}.
This contains a BOOT.BIN file produced with the APx Image Builder.

%install
rm -rf %{buildroot}
mkdir -p %{buildroot}/boot/fw
cp BOOT.BIN %{buildroot}/boot/fw/BOOT.BIN
cp boot.scr.ub %{buildroot}/boot/boot.scr

%clean
rm -rf %{buildroot}

%post
sync
sync

%files
%defattr (-, root, root)
/boot/fw/BOOT.BIN
/boot/boot.scr
