build:
	echo 'Use "make deb" or "make rpm"'

rpm:
	python3 setup.py bdist_rpm

deb:
	python3 setup.py --command-packages=stdeb.command bdist_deb

clean:
	rm -rf deb_dist dist apx_image_builder-*.tar.gz apx_image_builder.egg-info build
